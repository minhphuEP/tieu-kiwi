"""Seed `Requirement -impacts-> Component` edges from a hand-maintained YAML.

Input:  data_ingestion/impacts_map.yml  (see file header for format)
Output: edges rows with props_json._meta.extraction_source='human'
        and review_status='verified'.

Idempotent:
  - Existing edges get their props_json refreshed (JSON merge, not overwrite).
  - No duplicate edges — (src, rel, dst) triplet is checked before INSERT.
  - Unknown Requirement/Component refs are warned and skipped, not created.

No prune: removing a line from the yml does NOT delete the corresponding edge.
Add a --prune flag later if source-of-truth semantics are needed.

Usage:
    python scripts/ingest/impacts.py                    # default: data_ingestion/impacts_map.yml, project=CDM
    python scripts/ingest/impacts.py --project=CDM
    python scripts/ingest/impacts.py --file=path/to/other.yml --project=OTHER
    python scripts/ingest/impacts.py --dry-run          # print plan without writing
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

import argparse
from pathlib import Path

import psycopg
import yaml

from tieukiwi import db


DEFAULT_YAML = _P(__file__).resolve().parents[2] / "data_ingestion" / "impacts_map.yml"
EDGE_META = {
    "extraction_source": "human",
    "review_status": "verified",
    "confidence": 1.0,
    "source_file": "data_ingestion/impacts_map.yml",
}


def _load_yaml(path):
    doc = yaml.safe_load(Path(path).read_text()) or {}
    out = {}
    for jira_ref, comp_refs in doc.items():
        if not isinstance(comp_refs, list):
            print(f"[warn] {jira_ref}: value must be a list of Component refs, got {type(comp_refs).__name__} — skipped")
            continue
        clean = [c.strip() for c in comp_refs if isinstance(c, str) and c.strip()]
        if clean:
            out[jira_ref.strip()] = clean
    return out


def _find_node(cur, type_, ref, project_id):
    cur.execute(
        "SELECT id FROM nodes WHERE type=%s AND ref=%s AND project_id=%s",
        (type_, ref, project_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _upsert_edge(cur, src_id, rel, dst_id, props):
    cur.execute(
        "SELECT id FROM edges WHERE src_id=%s AND rel=%s AND dst_id=%s",
        (src_id, rel, dst_id),
    )
    row = cur.fetchone()
    payload = psycopg.types.json.Json(props)
    if row:
        cur.execute(
            "UPDATE edges SET props_json = props_json || %s::jsonb WHERE id = %s",
            (payload, row[0]),
        )
        return "updated"
    cur.execute(
        "INSERT INTO edges (src_id, rel, dst_id, props_json) VALUES (%s, %s, %s, %s)",
        (src_id, rel, dst_id, payload),
    )
    return "inserted"


def run(yaml_path, project_id, dry_run=False):
    mapping = _load_yaml(yaml_path)
    if not mapping:
        print(f"[warn] {yaml_path} is empty — nothing to do.")
        return

    inserted = updated = skipped_req = skipped_comp = 0

    with db.conn() as c:
        cur = c.cursor()
        for jira_ref, comp_refs in mapping.items():
            req_id = _find_node(cur, "Requirement", jira_ref, project_id)
            if req_id is None:
                print(f"[skip] Requirement {jira_ref} not found in project {project_id} — ingest the ticket first.")
                skipped_req += 1
                continue
            print(f"[req]  {jira_ref} (id={req_id}) -> {len(comp_refs)} component(s)")
            for comp_ref in comp_refs:
                comp_id = _find_node(cur, "Component", comp_ref, project_id)
                if comp_id is None:
                    print(f"  [skip] Component {comp_ref} not found in project {project_id}")
                    skipped_comp += 1
                    continue
                if dry_run:
                    cur.execute(
                        "SELECT 1 FROM edges WHERE src_id=%s AND rel='impacts' AND dst_id=%s",
                        (req_id, comp_id),
                    )
                    exists = cur.fetchone() is not None
                    if exists:
                        updated += 1
                        print(f"  [=]    impacts -> {comp_ref}  (would refresh)")
                    else:
                        inserted += 1
                        print(f"  [+]    impacts -> {comp_ref}  (would insert)")
                    continue
                action = _upsert_edge(cur, req_id, "impacts", comp_id, {"_meta": EDGE_META})
                if action == "inserted":
                    inserted += 1
                    print(f"  [+]    impacts -> {comp_ref}")
                else:
                    updated += 1
                    print(f"  [=]    impacts -> {comp_ref}  (props refreshed)")
        if dry_run:
            c.rollback()
            print("\n[dry-run] rolled back — no changes committed.")
        else:
            c.commit()

    print(f"\nSummary: {inserted} inserted, {updated} refreshed, "
          f"{skipped_req} req skipped, {skipped_comp} component skipped.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", default=str(DEFAULT_YAML),
                    help=f"YAML mapping file (default: {DEFAULT_YAML.relative_to(_P(__file__).resolve().parents[2])})")
    ap.add_argument("--project", default="CDM", help="Project id (default: CDM)")
    ap.add_argument("--dry-run", action="store_true", help="Print plan; do not write.")
    args = ap.parse_args()
    run(args.file, args.project, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
