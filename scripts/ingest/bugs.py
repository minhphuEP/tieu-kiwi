"""Ingest a bug from a Jira export (.json OR .doc/.docx/.txt) into the graph.

Supported inputs:
  - .json   Jira REST-shaped {"issues": [...]}    (multi-issue batch)
  - .doc    Jira "Export as Word" file            (single issue; needs macOS textutil)
  - .docx   Jira "Export as Word" file            (single issue; python-docx)
  - .txt    plain-text Jira export                (single issue)

For .doc / .docx / .txt the raw text is sent to the LLM (see tieukiwi/llm.py)
which returns a structured JSON. Then we create:
  - Bug node with ref, severity, status, assignee, ...
  - `violates` edges to any AcceptanceCriterion refs it mentions
  - `affects` edges to any Component names it mentions
  - `finds` edges from a TestRun if a run ref is present (optional)

Usage:
    python scripts/ingest/bugs.py path/to/CDM-287.doc --project=CDM
    python scripts/ingest/bugs.py path/to/export.json --project=CDM
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

import argparse
import json
from pathlib import Path

import psycopg

from tieukiwi import db
from tieukiwi.llm import complete_json
from tieukiwi.text_extract import read_text as _read_doc_text
from tieukiwi.config import LLM_PROVIDER, ANTHROPIC_MODEL, OLLAMA_LLM_MODEL


SYSTEM_PROMPT = """\
You are an information extraction engine for a QE (Quality Engineering) agent.

You will receive one Jira bug export (text). Extract exactly ONE bug.

Output shape (JSON, no prose):
{
  "ref": "<Jira key, e.g. 'CDM-287'>",
  "summary": "<short bug title>",
  "severity": "<critical | high | medium | low  — map from Priority / Severity>",
  "status": "<lowercase, e.g. 'open' | 'in_progress' | 'done' | 'closed'>",
  "reporter": "<person name string, may be Vietnamese>",
  "assignee": "<person name string, may be Vietnamese>",
  "sprint": "<sprint code if present>",
  "parent_ref": "<parent story key if present, e.g. 'CDM-198'>",
  "description": {
    "bug":         "<summary of the bug (Vietnamese OK)>",
    "steps":       "<repro steps>",
    "actual":      "<actual result>",
    "expected":    "<expected result>",
    "root_cause":  "<if present, else null>",
    "find_by":     "<how bug was found, if present, else null>"
  },
  "violates_ac_refs":     ["<AC refs the bug clearly violates, if mentioned>"],
  "affects_components":   ["<component/service names the bug clearly affects>"],
  "found_by_testrun_ref": "<TestRun ref if mentioned, else null>"
}

Rules:
- Preserve original language (Vietnamese stays Vietnamese).
- Do not invent AC refs / component names — only if clearly stated in the text.
- If a field is truly unknown, use null (not empty string).
- Return valid JSON only. No markdown fences, no prose.
"""


SEVERITY_MAP = {
    "highest": "critical", "blocker": "critical", "critical": "critical",
    "high":    "high",     "major":   "high",
    "medium":  "medium",   "normal":  "medium",
    "low":     "low",      "minor":   "low",     "lowest": "low",
    "trivial": "low",
}


def read_bug_source(path):
    """Return either a list[str] of raw texts (each = one issue) OR a dict for JSON batch.

    Text formats (.doc/.docx/.pdf/.md/.txt) are extracted by tieukiwi.text_extract.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".json":
        return {"kind": "json", "data": json.loads(p.read_text(encoding="utf-8"))}
    if suffix in (".doc", ".docx", ".pdf", ".txt", ".md", ".markdown"):
        return {"kind": "text", "texts": [_read_doc_text(p)]}
    raise ValueError(f"Unsupported extension: {suffix}")


def extract_from_text(text):
    """Run LLM to turn a Jira bug export into structured JSON."""
    model = ANTHROPIC_MODEL if LLM_PROVIDER == "anthropic" else OLLAMA_LLM_MODEL
    print(f"[info] Calling {LLM_PROVIDER}:{model} for bug extraction "
          f"(~{len(text)} chars)...")
    data = complete_json(text, system=SYSTEM_PROMPT, max_tokens=3000, temperature=0.1)
    if "ref" not in data:
        raise ValueError(f"LLM output missing 'ref': {list(data.keys())}")
    return data


def _upsert_node(cur, type_, ref, project_id, props):
    cur.execute(
        """
        INSERT INTO nodes (type, ref, project_id, props_json)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (project_id, ref) WHERE ref IS NOT NULL DO UPDATE
          SET props_json = nodes.props_json || EXCLUDED.props_json
        RETURNING id
        """,
        (type_, ref, project_id, psycopg.types.json.Json(props)),
    )
    return cur.fetchone()[0]


def _find_node_id(cur, type_, ref, project_id):
    cur.execute(
        "SELECT id FROM nodes WHERE type=%s AND ref=%s AND project_id=%s",
        (type_, ref, project_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _ensure_edge(cur, src_id, rel, dst_id):
    cur.execute(
        """
        INSERT INTO edges (src_id, rel, dst_id)
        SELECT %s, %s, %s
        WHERE NOT EXISTS (
          SELECT 1 FROM edges WHERE src_id=%s AND rel=%s AND dst_id=%s
        )
        """,
        (src_id, rel, dst_id, src_id, rel, dst_id),
    )


def ingest_bug(data, project_id, source_file):
    """Materialise one extracted bug dict into nodes/edges. Idempotent."""
    ref = data["ref"]
    model = ANTHROPIC_MODEL if LLM_PROVIDER == "anthropic" else OLLAMA_LLM_MODEL
    severity = SEVERITY_MAP.get(str(data.get("severity", "")).lower(), data.get("severity"))

    bug_props = {
        "summary":  data.get("summary"),
        "severity": severity,
        "status":   (data.get("status") or "").lower() or None,
        "reporter": data.get("reporter"),
        "assignee": data.get("assignee"),
        "sprint":   data.get("sprint"),
        "parent_ref": data.get("parent_ref"),
        "description": data.get("description"),
        "origin":  "testing",   # jira exports are almost always found via testing
        "_meta": {
            "extraction_source": f"llm:{model}",
            "confidence": 0.85,
            "source_file": str(source_file),
            "review_status": "draft",
        },
    }

    with db.conn() as c:
        cur = c.cursor()
        bug_id = _upsert_node(cur, "Bug", ref, project_id, bug_props)
        print(f"[bug] {ref} — {data.get('summary','')[:70]}")

        # Parent UserStory (auto-create if missing)
        if data.get("parent_ref"):
            us_id = _upsert_node(cur, "UserStory", data["parent_ref"], project_id, {
                "_meta": {"extraction_source": "bug-parent-ref",
                          "source_file": str(source_file)},
            })
            _ensure_edge(cur, us_id, "has", bug_id)

        # violates → AC refs (only link if AC exists; do not auto-create ACs)
        for ac_ref in data.get("violates_ac_refs") or []:
            ac_id = _find_node_id(cur, "AcceptanceCriterion", ac_ref, project_id)
            if ac_id:
                _ensure_edge(cur, bug_id, "violates", ac_id)
                print(f"  [violates] {ac_ref}")
            else:
                print(f"  [warn] violates_ac_refs mentions {ac_ref} but node not found — skipped")

        # affects → Component (auto-create by name)
        for comp_name in data.get("affects_components") or []:
            if not comp_name or not isinstance(comp_name, str):
                continue
            comp_ref = "COMP-" + comp_name.replace(" ", "-").replace("/", "-")[:40]
            comp_id = _upsert_node(cur, "Component", comp_ref, project_id, {
                "name": comp_name,
                "_meta": {"extraction_source": f"llm:{model}",
                          "source_file": str(source_file),
                          "note": "auto-created by ingest_bugs"},
            })
            _ensure_edge(cur, bug_id, "affects", comp_id)
            print(f"  [affects] {comp_ref}")

        # finds ← TestRun (only if run exists)
        tr_ref = data.get("found_by_testrun_ref")
        if tr_ref:
            tr_id = _find_node_id(cur, "TestRun", tr_ref, project_id)
            if tr_id:
                _ensure_edge(cur, tr_id, "finds", bug_id)
                print(f"  [finds] linked from {tr_ref}")


def ingest(file_path, project_id):
    src = read_bug_source(file_path)
    if src["kind"] == "text":
        for text in src["texts"]:
            data = extract_from_text(text)
            ingest_bug(data, project_id, file_path)
    elif src["kind"] == "json":
        issues = src["data"].get("issues") or []
        for issue in issues:
            # If the JSON is already Jira REST shape, we still push it through
            # the LLM to normalise. (Alternative: field-map directly — future work.)
            data = extract_from_text(json.dumps(issue, ensure_ascii=False))
            ingest_bug(data, project_id, file_path)
    print(f"\n[done] Ingested from {file_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("file", help="Path to Jira export (.json | .doc | .docx | .txt)")
    ap.add_argument("--project", required=True, help="project_id")
    args = ap.parse_args()

    if not Path(args.file).exists():
        raise SystemExit(f"File not found: {args.file}")
    ingest(args.file, args.project)


if __name__ == "__main__":
    main()
