"""Ingest a requirement / BRD document into the graph.

Supported input formats (auto-detected by extension):
  .md / .markdown / .txt
  .pdf   (requires pypdf)
  .docx  (requires python-docx)
  .doc   (macOS only via textutil — convert to .docx elsewhere)

Sends the whole file to the LLM (see tieukiwi/llm.py) and asks it to extract:
  - a single Requirement (ref + title + user story)
  - N AcceptanceCriteria (ref + title + full detail)
  - a list of Component names mentioned (auto-created as `Component` nodes,
    linked via `impacts` edges to the Requirement)

The LLM output is stored under `props_json._meta.extraction_source=llm:<model>`
and `review_status=draft` — a human should mark it `verified` after review.

Usage:
    python scripts/ingest/requirements.py path/to/file.<md|pdf|docx> \\
        --project=CDM \\
        --sprint=SPR-26W7 \\
        --us=US-14 \\
        --us-title="Duplicate script"

--sprint / --us / --us-title are optional; if omitted the Requirement is a
loose node (no parent Sprint/UserStory).

Requires LLM_PROVIDER + ANTHROPIC_API_KEY (or Ollama) in .env.
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
from tieukiwi.text_extract import read_text
from tieukiwi.config import LLM_PROVIDER, ANTHROPIC_MODEL, OLLAMA_LLM_MODEL


SYSTEM_PROMPT = """\
You are an information extraction engine for a QE (Quality Engineering) agent.

You will receive one requirement / BRD document written in Vietnamese or English.
Extract exactly ONE Requirement plus its Acceptance Criteria.

Output shape (JSON, no prose):
{
  "requirement": {
    "ref":    "<short stable code, prefer any explicit ticket key in the doc,
                else derive from title, e.g. 'REQ-DUPLICATE-SCRIPT'>",
    "title":  "<short title, <= 80 chars>",
    "detail": "<full user story / description, keep original language>"
  },
  "acs": [
    {
      "ref":    "<AC-1 | AC-2 | ... — use numbering from doc if present>",
      "title":  "<short title, <= 100 chars>",
      "detail": "<full text of this acceptance criterion, keep original language;
                 include sub-bullets and tables inline>"
    }
  ],
  "components": [
    "<component/service name mentioned in the doc, e.g. 'auth-service'>"
  ]
}

Rules:
- Preserve original language (Vietnamese stays Vietnamese, English stays English).
- If the doc references a ticket key like 'CDM-198', 'REQ-42', 'US-101',
  use that as the requirement.ref.
- If no ACs are explicit, split logical clauses into ACs and label AC-1..AC-N.
- Components: only mention names that clearly refer to backend services or
  UI modules (skip generic words like "system", "user").
- Return valid JSON only. No markdown fences, no leading/trailing prose.
"""


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


def extract(text):
    """Call the LLM and parse to a dict. Fails hard if JSON invalid."""
    model = ANTHROPIC_MODEL if LLM_PROVIDER == "anthropic" else OLLAMA_LLM_MODEL
    print(f"[info] Calling {LLM_PROVIDER}:{model} for extraction "
          f"(~{len(text)} chars input)...")
    data = complete_json(text, system=SYSTEM_PROMPT, max_tokens=4000, temperature=0.1)
    if "requirement" not in data or "acs" not in data:
        raise ValueError(f"LLM output missing required fields: {list(data.keys())}")
    return data


def ingest(file_path, project_id, sprint_ref=None, us_ref=None, us_title=None):
    text = read_text(file_path)
    if not text.strip():
        raise SystemExit(f"[error] {file_path}: no extractable text found")
    data = extract(text)

    req = data["requirement"]
    acs = data.get("acs", [])
    components = data.get("components", [])

    model = ANTHROPIC_MODEL if LLM_PROVIDER == "anthropic" else OLLAMA_LLM_MODEL
    meta_common = {
        "extraction_source": f"llm:{model}",
        "confidence": 0.85,
        "source_file": str(file_path),
        "review_status": "draft",
    }

    with db.conn() as c:
        cur = c.cursor()

        # Ensure Sprint (optional)
        sprint_id = None
        if sprint_ref:
            sprint_id = _upsert_node(cur, "Sprint", sprint_ref, project_id, {
                "_meta": {"extraction_source": "cli-arg", "source_file": str(file_path)},
            })

        # Ensure UserStory (optional)
        us_id = None
        if us_ref:
            us_props = {"_meta": {"extraction_source": "cli-arg",
                                  "source_file": str(file_path)}}
            if us_title:
                us_props["title"] = us_title
            us_id = _upsert_node(cur, "UserStory", us_ref, project_id, us_props)
            if sprint_id:
                _ensure_edge(cur, sprint_id, "has", us_id)

        # Requirement
        req_props = {
            "title":  req.get("title"),
            "detail": req.get("detail"),
            "_meta":  meta_common,
        }
        req_id = _upsert_node(cur, "Requirement", req["ref"], project_id, req_props)
        if us_id:
            _ensure_edge(cur, us_id, "has", req_id)
        print(f"[req] {req['ref']} — {req.get('title','')[:60]}")

        # ACs
        for i, ac in enumerate(acs, start=1):
            ac_ref = ac.get("ref") or f"{req['ref']}-AC-{i}"
            ac_props = {
                "title":  ac.get("title"),
                "detail": ac.get("detail"),
                "_meta":  meta_common,
            }
            ac_id = _upsert_node(cur, "AcceptanceCriterion", ac_ref, project_id, ac_props)
            _ensure_edge(cur, req_id, "has", ac_id)
            print(f"  [ac] {ac_ref} — {ac.get('title','')[:70]}")

        # Components → impacts
        for name in components:
            if not name or not isinstance(name, str):
                continue
            comp_ref = "COMP-" + name.replace(" ", "-").replace("/", "-")[:40]
            comp_props = {
                "name": name,
                "_meta": {"extraction_source": f"llm:{model}",
                          "source_file": str(file_path),
                          "note": "auto-created by ingest_requirements"},
            }
            comp_id = _upsert_node(cur, "Component", comp_ref, project_id, comp_props)
            _ensure_edge(cur, req_id, "impacts", comp_id)
            print(f"  [impacts] {comp_ref}")

    print(f"\n[done] Ingested requirement '{req['ref']}' with "
          f"{len(acs)} AC(s), {len(components)} component(s).")


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("file", help="Path to requirement (.md|.markdown|.txt|.pdf|.docx|.doc)")
    ap.add_argument("--project", required=True, help="project_id (Jira key prefix, e.g. CDM)")
    ap.add_argument("--sprint", default=None, help="Optional Sprint ref (auto-created)")
    ap.add_argument("--us", default=None, help="Optional UserStory ref (auto-created)")
    ap.add_argument("--us-title", default=None, help="UserStory title (used when --us is given)")
    args = ap.parse_args()

    if not Path(args.file).exists():
        raise SystemExit(f"File not found: {args.file}")
    ingest(args.file, args.project, args.sprint, args.us, args.us_title)


if __name__ == "__main__":
    main()
