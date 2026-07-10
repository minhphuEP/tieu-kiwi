"""LLM-propose AC↔TestCase links for a Requirement.

For each TestCase already in Postgres (linked to a Requirement via title/scope),
show the LLM all ACs of that Requirement + the TC content, and ask which
ACs the TC covers. Prints a dry-run diff for human review.

Use `--apply` to create the `AC ─coveredBy→ TestCase` edges. Each edge is
tagged `_meta.review_status='draft'` + `extraction_source='llm-tc-ac-match'`
so a curator can promote to 'verified' later.

Usage:
    .venv/bin/python scripts/dev/propose_tc_ac_links.py \\
        --requirement CDM-268 \\
        --project CDM
    # add --apply to write edges to DB
"""
import argparse
import json
import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv
load_dotenv(_P(__file__).resolve().parents[2] / ".env")

import os
import psycopg
from tieukiwi import db
from tieukiwi.config import ANTHROPIC_MODEL


SYSTEM_PROMPT = """You are a QE traceability engine. Your ONLY job is to \
produce a mapping from TestCases to the Acceptance Criteria they cover.

You will receive:
  1) A list of AC refs and their descriptions.
  2) A list of TestCases (ref + title + preconditions + steps + expected).

You must output ONE JSON object with a `mappings` array. Each item in the \
array represents ONE TestCase and lists which ACs it covers.

STRICT OUTPUT SCHEMA (all keys required, no others):
{
  "mappings": [
    {
      "tc_ref":   "<exact TC ref from input, e.g. CDM_AssignCreator_001>",
      "ac_refs":  ["AC-CDM-268-xxxxxxxx", ...],
      "reason":   "<1-2 sentences citing specific TC steps/expected>"
    }
  ]
}

RULES:
- The array MUST have exactly ONE item per input TestCase (no more, no less).
- ac_refs may be empty [] if no AC matches. Set reason='no AC clearly matches'.
- ac_refs MUST come from the provided AC list. Do NOT invent refs.
- Match by SCENARIO / BEHAVIOR VERIFIED (not by keyword overlap).
- Do NOT output TC details, steps, expected — only the mapping.
- Do NOT wrap in code fences. Do NOT add prose before or after the JSON.

EXAMPLE output for a hypothetical Requirement with 2 ACs and 2 TCs:
{
  "mappings": [
    {"tc_ref": "TC_Login_001", "ac_refs": ["AC-XYZ-1"], "reason": "TC steps 1-3 verify successful login flow described in AC-XYZ-1."},
    {"tc_ref": "TC_Login_002", "ac_refs": ["AC-XYZ-1", "AC-XYZ-2"], "reason": "TC covers both happy-path (AC-1) and error toast (AC-2)."}
  ]
}
"""


def _fetch_requirement(req_ref, project_id):
    with db.conn() as c:
        row = c.execute(
            "SELECT id, ref, props_json FROM nodes "
            "WHERE type='Requirement' AND ref=%s AND project_id=%s",
            (req_ref, project_id),
        ).fetchone()
    if not row:
        raise SystemExit(f"[error] Requirement {req_ref} not found in project {project_id}")
    return {"id": row[0], "ref": row[1], "props": row[2] or {}}


def _fetch_acs(req_id, project_id):
    with db.conn() as c:
        rows = c.execute(
            "SELECT ac.id, ac.ref, ac.props_json "
            "FROM edges h "
            "JOIN nodes ac ON ac.id=h.dst_id AND ac.type='AcceptanceCriterion' "
            "WHERE h.src_id=%s AND h.rel='has' AND ac.project_id=%s "
            "ORDER BY ac.ref",
            (req_id, project_id),
        ).fetchall()
    return [{"id": r[0], "ref": r[1], "desc": (r[2] or {}).get("desc","")} for r in rows]


def _fetch_testcases(project_id, tc_refs=None):
    """Fetch TCs by explicit list of refs. If None, fetch ALL TCs in project."""
    with db.conn() as c:
        if tc_refs:
            rows = c.execute(
                "SELECT id, ref, props_json FROM nodes "
                "WHERE type='TestCase' AND project_id=%s AND ref = ANY(%s) ORDER BY ref",
                (project_id, list(tc_refs)),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, ref, props_json FROM nodes "
                "WHERE type='TestCase' AND project_id=%s ORDER BY ref",
                (project_id,),
            ).fetchall()
    out = []
    for tid, tref, props in rows:
        props = props or {}
        out.append({
            "id":            tid,
            "ref":           tref,
            "title":         props.get("title","") or "",
            "priority":      props.get("priority","") or "",
            "preconditions": props.get("preconditions","") or "",
            "steps":         props.get("steps","") or "",
            "expected":      props.get("expected","") or "",
        })
    return out


def _call_anthropic_tool(user_prompt, acs, tcs):
    """Call Anthropic messages API with tool_use for strict schema enforcement.

    Falls back to a normal text call if tool_use not available.
    """
    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    ac_refs = [a["ref"] for a in acs]
    tc_refs = [t["ref"] for t in tcs]
    tool_schema = {
        "name": "submit_tc_ac_mappings",
        "description": "Submit the mapping of each TestCase to the AC refs it covers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mappings": {
                    "type": "array",
                    "description": (
                        f"Exactly {len(tc_refs)} items, one per TestCase in the input, "
                        f"in the same order. TC refs must be from: {tc_refs}. "
                        f"AC refs must be from: {ac_refs} (may be empty [])."
                    ),
                    "minItems": len(tc_refs),
                    "maxItems": len(tc_refs),
                    "items": {
                        "type": "object",
                        "properties": {
                            "tc_ref":  {"type": "string", "enum": tc_refs,
                                        "description": "The exact TestCase ref from input."},
                            "ac_refs": {"type": "array",
                                        "items": {"type": "string", "enum": ac_refs},
                                        "description": "AC refs this TC covers. [] if none."},
                            "reason":  {"type": "string",
                                        "description": "1-2 sentences citing specific TC steps/expected."},
                        },
                        "required": ["tc_ref", "ac_refs", "reason"],
                    },
                },
            },
            "required": ["mappings"],
        },
    }

    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=8192,
        temperature=0.2,
        tools=[tool_schema],
        tool_choice={"type": "tool", "name": "submit_tc_ac_mappings"},
        system="You are a QE traceability engine. Map each TestCase to the "
               "AC refs it covers. Call the submit_tc_ac_mappings tool with your answer.",
        messages=[{"role": "user", "content": user_prompt}],
    )
    # Extract tool_use block
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_tc_ac_mappings":
            return block.input
    raise SystemExit("[error] LLM did not call the tool. Content: "
                     + repr([getattr(b, 'type', '?') for b in msg.content]))


def _truncate(s, n):
    if not s: return ""
    s = str(s).strip()
    return s if len(s) <= n else s[:n].rstrip() + " …[truncated]"


def _build_prompt(req, acs, tcs, per_field_max=1200):
    tc_refs_list = ", ".join(t["ref"] for t in tcs)
    parts = [
        f"TASK: Map each TestCase below to the AC refs it covers. Output the "
        f"strict JSON schema described in the system prompt. Your output must "
        f"contain EXACTLY {len(tcs)} items in the `mappings` array — one per "
        f"TestCase in this order: {tc_refs_list}.",
        "",
        f"Requirement: {req['ref']} — {req['props'].get('title','') or '<no title>'}",
        "",
        f"### Acceptance Criteria ({len(acs)} total)",
    ]
    for a in acs:
        parts.append(f"  {a['ref']}: {a['desc']}")
    parts.append("")
    parts.append(f"### TestCases ({len(tcs)} total) — provided for you to map, NOT to summarize")
    for t in tcs:
        parts.append(f"\n#### TC {t['ref']}")
        parts.append(f"title:         {t['title']}")
        parts.append(f"priority:      {t['priority']}")
        parts.append(f"preconditions: {_truncate(t['preconditions'], per_field_max)}")
        parts.append(f"steps:         {_truncate(t['steps'], per_field_max)}")
        parts.append(f"expected:      {_truncate(t['expected'], per_field_max)}")
    parts.append("")
    parts.append("=" * 60)
    parts.append(f"Now output the JSON mapping object. Remember: exactly "
                 f"{len(tcs)} items in `mappings`, one per TC listed above, "
                 f"in the same order. Only refs from the AC list. Output the "
                 f"JSON object NOW.")
    return "\n".join(parts)


def _apply(mappings, project_id):
    """Create AC-coveredBy-TC edges. Idempotent (skip if edge exists)."""
    created, skipped = 0, 0
    with db.conn() as c:
        for m in mappings:
            tc_ref = m["tc_ref"]
            ac_refs = m.get("ac_refs") or []
            tc_row = c.execute(
                "SELECT id FROM nodes WHERE type='TestCase' AND ref=%s AND project_id=%s",
                (tc_ref, project_id),
            ).fetchone()
            if not tc_row:
                skipped += len(ac_refs); continue
            tc_id = tc_row[0]
            for ac_ref in ac_refs:
                ac_row = c.execute(
                    "SELECT id FROM nodes WHERE type='AcceptanceCriterion' AND ref=%s AND project_id=%s",
                    (ac_ref, project_id),
                ).fetchone()
                if not ac_row:
                    skipped += 1; continue
                ac_id = ac_row[0]
                exists = c.execute(
                    "SELECT id FROM edges WHERE src_id=%s AND rel='coveredBy' AND dst_id=%s",
                    (ac_id, tc_id),
                ).fetchone()
                if exists:
                    skipped += 1; continue
                c.execute(
                    "INSERT INTO edges(src_id, rel, dst_id, props_json) VALUES (%s,'coveredBy',%s,%s)",
                    (ac_id, tc_id, psycopg.types.json.Json({
                        "reason": m.get("reason",""),
                        "_meta": {
                            "extraction_source": "llm-tc-ac-match",
                            "confidence": 0.75,
                            "review_status": "draft",
                            "matched_requirement": None,
                        },
                    })),
                )
                created += 1
    return created, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--requirement", required=True, help="Requirement ref, e.g. CDM-268")
    ap.add_argument("--project", default="CDM")
    ap.add_argument("--tc-prefix", default=None,
                    help="Optional filter: only match TCs whose ref starts with this "
                         "(e.g. 'CDM_DupScript_' or 'CDM_AssignCreator_'). Default: "
                         "all TCs in project.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually create coveredBy edges (default: dry-run only)")
    args = ap.parse_args()

    req = _fetch_requirement(args.requirement, args.project)
    acs = _fetch_acs(req["id"], args.project)
    tcs = _fetch_testcases(args.project)
    if args.tc_prefix:
        tcs = [t for t in tcs if t["ref"].startswith(args.tc_prefix)]
    if not acs:
        raise SystemExit(f"[error] Requirement {args.requirement} has no ACs")
    if not tcs:
        raise SystemExit(f"[error] No TestCases found (project={args.project}, prefix={args.tc_prefix})")

    print(f"[input] Requirement: {req['ref']}   ACs: {len(acs)}   TCs: {len(tcs)}")
    prompt = _build_prompt(req, acs, tcs)
    print(f"[input] Prompt size: {len(prompt)} chars")

    print("\n[llm] Asking Claude to propose mappings (tool_use for strict schema)...")
    result = _call_anthropic_tool(prompt, acs, tcs)
    _P("/tmp/llm_raw.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))

    mappings = result["mappings"]
    print(f"\n[proposed] {len(mappings)} TC mappings:\n")
    known_acs = {a["ref"] for a in acs}
    for m in mappings:
        tc_ref  = m.get("tc_ref","")
        ac_refs = m.get("ac_refs", []) or []
        reason  = m.get("reason","")
        # Validate AC refs exist
        unknown = [a for a in ac_refs if a not in known_acs]
        mark = "  " if not unknown else " ⚠"
        print(f"{mark} {tc_ref}")
        for ac in ac_refs:
            desc = next((a["desc"] for a in acs if a["ref"] == ac), "?")
            flag = " ⚠UNKNOWN" if ac in unknown else ""
            print(f"     → {ac}{flag}  — {desc[:80]}")
        if not ac_refs:
            print(f"     → (no AC matched)")
        print(f"     reason: {reason}")
        print()

    if args.apply:
        # Drop any mappings referencing unknown ACs (hallucinated)
        clean = []
        for m in mappings:
            good_refs = [a for a in (m.get("ac_refs") or []) if a in known_acs]
            if good_refs != (m.get("ac_refs") or []):
                print(f"[warn] Dropped hallucinated AC refs from {m.get('tc_ref')}")
            clean.append({**m, "ac_refs": good_refs})
        created, skipped = _apply(clean, args.project)
        print(f"\n[apply] Created {created} coveredBy edges, skipped {skipped} (already existed / missing node).")
    else:
        print("[dry-run] Not writing to DB. Re-run with --apply to persist.")


if __name__ == "__main__":
    main()
