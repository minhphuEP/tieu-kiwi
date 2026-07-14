"""Seed CDM feature-level Components + `dependsOn` edges from YAML.

Reads the single source of truth `kb/CDM/components.yml` (component ontology +
business-level dependency graph, hand-authored with PO / tech lead).

Model: Component = a BUSINESS FEATURE / user-facing capability, not a service
or code module. `dependsOn` edges are hand-authored, never extracted from
code imports.

Idempotent: upserts Components (ON CONFLICT via migration 003's unique index);
edges deduped by (src, rel, dst).

Usage:
    .venv/bin/python scripts/seed/cdm_components.py
    .venv/bin/python scripts/seed/cdm_components.py --components kb/CDM/components.yml
    .venv/bin/python scripts/seed/cdm_components.py --check-only

Requirements:
    - DATABASE_URL set (see .env.example)
    - Schema + migrations 002/003/004 applied
    - PyYAML installed (already in requirements.txt via .venv)
"""
import argparse
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

import yaml

from tieukiwi import db


PROJECT = "CDM"
DEFAULT_YAML = _P(__file__).resolve().parents[2] / "kb" / "CDM" / "components.yml"


def _meta_human_seed(source_file: str):
    return {"_meta": {
        "extraction_source": "human-seed",
        "confidence": 1.0,
        "review_status": "verified",
        "source_file": source_file,
    }}


def _load(yaml_path: _P):
    doc = yaml.safe_load(yaml_path.read_text())
    if not doc or "components" not in doc:
        raise SystemExit(f"[error] {yaml_path} missing top-level `components:` key")
    components = doc["components"]
    depends_on = doc.get("depends_on", []) or []
    if not isinstance(components, list):
        raise SystemExit(f"[error] `components` must be a list, got {type(components).__name__}")
    return components, depends_on


def seed(yaml_path: _P):
    components, depends_on = _load(yaml_path)
    src_marker = str(yaml_path.relative_to(_P(__file__).resolve().parents[2]))

    # 1) Upsert Components
    id_by_ref = {}
    for c in components:
        ref = c.get("ref")
        if not ref:
            raise SystemExit(f"[error] Component missing `ref`: {c}")
        props = {
            **_meta_human_seed(src_marker),
            "name":        c.get("name") or ref,
            "description": (c.get("description") or "").strip(),
            "owners":      c.get("owners") or [],
            "source":      (c.get("source") or "").strip(),
        }
        nid = db.upsert_node_by_ref(
            "Component", ref, props=props, project_id=PROJECT, merge_props=True,
        )
        id_by_ref[ref] = nid

    # 2) Upsert dependsOn edges
    skipped = []
    ok = 0
    for e in depends_on:
        src = e.get("src")
        dst = e.get("dst")
        if not src or not dst:
            skipped.append((src, dst, "missing src or dst"))
            continue
        src_id = id_by_ref.get(src) or db.node_id_for(src, type_="Component")
        dst_id = id_by_ref.get(dst) or db.node_id_for(dst, type_="Component")
        if src_id is None:
            skipped.append((src, dst, f"Component src not found: {src}"))
            continue
        if dst_id is None:
            skipped.append((src, dst, f"Component dst not found: {dst}"))
            continue
        db.ensure_edge(src_id, "dependsOn", dst_id, props={
            **_meta_human_seed(src_marker),
        })
        ok += 1

    return id_by_ref, ok, skipped


def _print_check():
    from tieukiwi.db import conn
    with conn() as c:
        comps = c.execute(
            "SELECT ref, props_json->>'name' FROM nodes "
            "WHERE type='Component' AND project_id=%s ORDER BY ref",
            (PROJECT,),
        ).fetchall()
        deps = c.execute(
            """
            SELECT s.ref, d.ref
            FROM edges e
            JOIN nodes s ON s.id=e.src_id AND s.type='Component' AND s.project_id=%s
            JOIN nodes d ON d.id=e.dst_id AND d.type='Component'
            WHERE e.rel='dependsOn'
            ORDER BY s.ref, d.ref
            """,
            (PROJECT,),
        ).fetchall()

    print(f"\n[{PROJECT}] Components ({len(comps)}):")
    for ref, name in comps:
        print(f"  {ref:36s} {name or '-'}")
    print(f"\n[{PROJECT}] dependsOn edges ({len(deps)}):")
    for s, d in deps:
        print(f"  {s:36s} → {d}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--components", type=_P, default=DEFAULT_YAML,
                    help=f"path to components.yml (default: {DEFAULT_YAML})")
    ap.add_argument("--check-only", action="store_true",
                    help="don't seed, just print current DB state")
    args = ap.parse_args()

    if args.check_only:
        _print_check()
        return

    ids, ok, skipped = seed(args.components)
    print(f"[seed] Components upserted: {len(ids)}")
    print(f"[seed] dependsOn edges:     {ok} ok, {len(skipped)} skipped")
    if skipped:
        print("[seed] Skipped:")
        for s, d, reason in skipped:
            print(f"  {s} → {d}  ({reason})")
    _print_check()


if __name__ == "__main__":
    main()
