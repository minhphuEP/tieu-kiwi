"""Ingest a code graph (graphify-out/graph.json) into Tieu Kiwi's Postgres.

Reads a `graph.json` produced by graphify + a `component_code_map.yml`
mapping business Components to file globs, and creates:

  - `CodeUnit` nodes (type='CodeUnit', ref='<project>:<graph_node_id>')
  - Code edges (contains, imports, imports_from, calls, references, ...)
    exactly as authored by graphify — direction preserved.
  - `Component -implementedBy-> CodeUnit` edges via glob resolution.

Idempotent: nodes use ON CONFLICT (project_id, ref); edges use WHERE NOT EXISTS.
Re-run safely after `graphify update .`.

Stale detection: CodeUnits whose stored `_meta.built_at_commit` differs from
graph.json's `built_at_commit` are marked `review_status='stale'` (kept, not
deleted) so old references stay traceable.

Test files (*.test.tsx / *.test.ts) are EXCLUDED at ingest per user decision;
add later if we want FE test coverage tracking.

Preconditions:
  - Components already seeded (`scripts/seed/cdm_components.py`). If a Component
    ref in the map has no matching node, its implementedBy edges are SKIPPED
    and a warning printed.

Usage:
  .venv/bin/python scripts/ingest/code_graph.py \\
    --graph /path/to/graphify-out/graph.json \\
    --map   kb/CDM/component_code_map.yml \\
    --project-id CDM

Optional flags:
  --dry-run       Print what would happen; don't touch DB.
  --skip-code-edges  Only ingest CodeUnit + implementedBy (fast smoke test).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path as _P
from collections import defaultdict, Counter
from typing import Optional, Dict, List, Set, Tuple

sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

import json
import yaml

from tieukiwi import db


DEFAULT_PROJECT = "CDM"
DEFAULT_MAP    = _P(__file__).resolve().parents[2] / "kb" / "CDM" / "component_code_map.yml"

TEST_SUFFIXES  = (".test.tsx", ".test.ts")


def _is_test_file(sf: str) -> bool:
    """True if `sf` looks like a test file — skipped from ingest.

    Handles both FE (`*.test.tsx`, `*.test.ts`) and BE (Python) conventions
    (`test_*.py`, `conftest.py`, anything under `/tests/`).
    """
    if not sf:
        return False
    if sf.endswith(TEST_SUFFIXES):
        return True
    if "/tests/" in sf:
        return True
    basename = sf.rsplit("/", 1)[-1]
    if basename == "conftest.py":
        return True
    if basename.startswith("test_") and basename.endswith(".py"):
        return True
    return False


# ---------------------------------------------------------------------------
# Glob helpers — same logic as the dry-run in kb/CDM/component_code_map.yml docs.
# ---------------------------------------------------------------------------

def _glob_to_re(pat: str) -> re.Pattern:
    """Translate a shell-like glob to a regex.

    Supports `**` (crosses path separators) and `*` (single segment) and `?`.
    """
    r = re.escape(pat)
    r = r.replace(r"\*\*", "§§")      # temp marker to protect **
    r = r.replace(r"\*", "[^/]*")     # single-segment wildcard
    r = r.replace("§§", ".*")          # ** becomes multi-segment
    r = r.replace(r"\?", ".")
    return re.compile("^" + r + "$")


def classify_file(rel_path: str, components: dict, overrides: dict) -> Optional[str]:
    """Return the Component ref this file belongs to, or None."""
    if rel_path in overrides:
        return overrides[rel_path]
    for comp_ref, globs in components.items():
        if any(_glob_to_re(p).match(rel_path) for p in globs):
            return comp_ref
    return None


# ---------------------------------------------------------------------------
# Ingest core
# ---------------------------------------------------------------------------

def _code_ref(project_id: str, node_id: str) -> str:
    """CodeUnit ref = `<project_id>:<graph_node.id>` — namespaced per project."""
    return f"{project_id}:{node_id}"


def _load_yaml_map(path: _P):
    doc = yaml.safe_load(path.read_text()) or {}
    overrides = doc.pop("overrides", {}) or {}
    excluded_globs = doc.pop("excluded_globs", []) or []
    components = {k: v or [] for k, v in doc.items() if isinstance(v, list)}
    return components, overrides, excluded_globs


def _iter_graph_nodes(graph: dict):
    """Yield non-test CodeUnit nodes with normalized fields."""
    for n in graph.get("nodes", []):
        sf = n.get("source_file", "") or ""
        if _is_test_file(sf):
            continue
        yield n


def ingest_code_units(project_id: str, graph: dict, source_tag: Optional[str],
                       dry_run: bool) -> Tuple[dict, int, int]:
    """Upsert CodeUnit nodes. Returns (ref->id map, upserted, stale_marked).

    `source_tag` (e.g. 'frontend', 'backend') is stored in _meta.source_graph
    and used to SCOPE the stale sweep — so ingesting a BE graph doesn't
    accidentally mark all FE nodes as stale (they came from a different graph).
    """
    built_commit = graph.get("built_at_commit")
    id_by_ref: Dict[str, int] = {}
    upserted = 0
    stale = 0

    for n in _iter_graph_nodes(graph):
        gid  = n["id"]
        ref  = _code_ref(project_id, gid)
        props = {
            "label":           n.get("label"),
            "source_file":     n.get("source_file"),
            "source_location": n.get("source_location"),
            "community":       n.get("community"),
            "norm_label":      n.get("norm_label"),
            "_meta": {
                "extraction_source": "ast:graphify",
                "confidence":        1.0,
                "review_status":     "verified",
                "built_at_commit":   built_commit,
                "source_file":       n.get("source_file"),
                "source_graph":      source_tag,
            },
        }
        if dry_run:
            id_by_ref[ref] = -1
            continue

        nid = db.upsert_node_by_ref(
            "CodeUnit", ref, props=props, project_id=project_id, merge_props=True,
        )
        id_by_ref[ref] = nid
        upserted += 1

    # Stale sweep: only touch CodeUnits with matching source_graph. Without
    # this, running the BE ingest would mark all FE nodes as stale (they
    # weren't in the BE graph). If source_tag is None (legacy), skip the sweep.
    if not dry_run and built_commit and source_tag:
        stale = _mark_stale_code_units(
            project_id, source_tag=source_tag,
            present_refs=set(id_by_ref.keys()),
            current_commit=built_commit,
        )

    return id_by_ref, upserted, stale


def _mark_stale_code_units(project_id: str, source_tag: str,
                            present_refs: set, current_commit: str) -> int:
    """Flip _meta.review_status → 'stale' for CodeUnits absent from the new graph.

    Scoped: only affects CodeUnits with `_meta.source_graph == source_tag`. This
    lets FE and BE ingests coexist without wrongfully staling each other's nodes.
    """
    from tieukiwi.db import conn
    import psycopg
    with conn() as c:
        rows = c.execute(
            """
            SELECT id, ref, props_json
              FROM nodes
             WHERE type='CodeUnit' AND project_id=%s
               AND props_json->'_meta'->>'source_graph' = %s
            """,
            (project_id, source_tag),
        ).fetchall()
        n_marked = 0
        for row_id, ref, props in rows:
            props = props or {}
            meta  = props.get("_meta", {}) or {}
            if ref in present_refs:
                continue  # still present in current graph
            if meta.get("review_status") == "stale":
                continue  # already marked
            new_meta = {**meta, "review_status": "stale"}
            c.execute(
                "UPDATE nodes SET props_json = props_json || %s::jsonb WHERE id=%s",
                (psycopg.types.json.Json({"_meta": new_meta}), row_id),
            )
            n_marked += 1
    return n_marked


def ingest_code_edges(project_id: str, graph: dict, ref_to_id: dict[str, int],
                      dry_run: bool) -> tuple[int, int]:
    """Ingest all graph edges. Returns (inserted, skipped_missing)."""
    built_commit = graph.get("built_at_commit")
    inserted = 0
    skipped = 0
    excluded_test_edges = 0

    for e in graph.get("links", []):
        src_ref = _code_ref(project_id, e["source"])
        dst_ref = _code_ref(project_id, e["target"])
        src_id  = ref_to_id.get(src_ref)
        dst_id  = ref_to_id.get(dst_ref)
        if src_id is None or dst_id is None:
            # Either endpoint is a test file (skipped) or missing — count and move on.
            excluded_test_edges += 1
            continue
        rel = e.get("relation") or "related"
        props = {
            "confidence":       e.get("confidence"),
            "confidence_score": e.get("confidence_score"),
            "weight":           e.get("weight"),
            "source_location":  e.get("source_location"),
            "built_at_commit":  built_commit,
        }
        if dry_run:
            inserted += 1
            continue
        db.ensure_edge(src_id, rel, dst_id, props=props)
        inserted += 1

    return inserted, excluded_test_edges


def ingest_implemented_by(project_id: str, graph: dict, ref_to_id: dict[str, int],
                          components: dict, overrides: dict,
                          dry_run: bool) -> tuple[int, list, list, dict]:
    """Create Component -implementedBy-> CodeUnit edges.

    Returns (edges_created, missing_component_refs, unmapped_files, per_component_count).
    """
    # Build file→CodeUnit-ref index from the graph (skipping tests).
    file_to_refs: dict[str, list[str]] = defaultdict(list)
    for n in _iter_graph_nodes(graph):
        sf = n.get("source_file") or ""
        if sf:
            file_to_refs[sf].append(_code_ref(project_id, n["id"]))

    # Resolve component_id for each Component ref in the map
    comp_id_by_ref: dict[str, int] = {}
    missing: list[str] = []
    for comp_ref in list(components.keys()) + list(set(overrides.values())):
        if comp_ref in comp_id_by_ref:
            continue
        cid = db.node_id_for(comp_ref, type_="Component")
        if cid is None:
            missing.append(comp_ref)
        else:
            comp_id_by_ref[comp_ref] = cid

    edges = 0
    per_comp: Counter = Counter()
    unmapped_files: list[str] = []

    for sf, code_refs in file_to_refs.items():
        comp_ref = classify_file(sf, components, overrides)
        if comp_ref is None:
            unmapped_files.append(sf)
            continue
        comp_id = comp_id_by_ref.get(comp_ref)
        if comp_id is None:
            # Component missing from DB — skip
            continue
        for cref in code_refs:
            code_id = ref_to_id.get(cref)
            if code_id is None:
                continue
            if not dry_run:
                db.ensure_edge(comp_id, "implementedBy", code_id, props={
                    "matched_file": sf,
                    "_meta": {
                        "extraction_source": "glob:component_code_map.yml",
                        "confidence":        1.0,
                        "review_status":     "verified",
                    },
                })
            edges += 1
            per_comp[comp_ref] += 1

    return edges, sorted(set(missing)), sorted(set(unmapped_files)), dict(per_comp)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--graph", type=_P, required=True,
                    help="path to graph.json produced by graphify")
    ap.add_argument("--map", type=_P, default=DEFAULT_MAP,
                    help=f"path to component_code_map.yml (default: {DEFAULT_MAP})")
    ap.add_argument("--project-id", default=DEFAULT_PROJECT,
                    help=f"Tieu Kiwi project_id (default: {DEFAULT_PROJECT})")
    ap.add_argument("--source-tag", default=None,
                    help="Tag stored in _meta.source_graph (e.g. 'frontend', 'backend'). "
                         "Also SCOPES the stale sweep so ingesting one graph doesn't "
                         "wrongfully mark nodes from another graph as stale.")
    ap.add_argument("--dry-run", action="store_true",
                    help="don't touch DB, just count")
    ap.add_argument("--skip-code-edges", action="store_true",
                    help="skip contains/imports/calls edges (only CodeUnit + implementedBy)")
    args = ap.parse_args()

    if not args.graph.exists():
        raise SystemExit(f"[error] graph.json not found: {args.graph}")
    if not args.map.exists():
        raise SystemExit(f"[error] component_code_map.yml not found: {args.map}")

    graph = json.loads(args.graph.read_text())
    components, overrides, excluded_globs = _load_yaml_map(args.map)
    print(f"[cfg] project_id={args.project_id}  commit={graph.get('built_at_commit')}")
    print(f"[cfg] graph:  {args.graph}")
    print(f"[cfg] map:    {args.map}")
    print(f"[cfg] dry_run={args.dry_run}  skip_code_edges={args.skip_code_edges}")
    print()

    # 1) CodeUnit nodes
    print("[1/3] Upserting CodeUnit nodes...")
    ref_to_id, upserted, stale = ingest_code_units(
        args.project_id, graph, args.source_tag, args.dry_run,
    )
    print(f"      CodeUnits upserted: {upserted}")
    print(f"      Stale marked:       {stale}")

    # 2) Code edges
    if args.skip_code_edges:
        print("[2/3] SKIPPED code edges (--skip-code-edges)")
    else:
        print("[2/3] Upserting code edges...")
        inserted, excluded = ingest_code_edges(args.project_id, graph, ref_to_id, args.dry_run)
        print(f"      Code edges upserted: {inserted}")
        print(f"      Edges touching test files (skipped): {excluded}")

    # 3) implementedBy edges
    print("[3/3] Upserting Component -implementedBy-> CodeUnit edges...")
    ib_edges, missing, unmapped, per_comp = ingest_implemented_by(
        args.project_id, graph, ref_to_id, components, overrides, args.dry_run,
    )
    print(f"      implementedBy edges: {ib_edges}")
    if missing:
        print(f"      [warn] Components referenced in map but MISSING from DB:")
        for m in missing:
            print(f"        - {m}   (run scripts/seed/cdm_components.py first)")

    if per_comp:
        print("\n      Per-Component implementedBy count:")
        for comp_ref in components.keys():
            print(f"        {comp_ref:36s}  {per_comp.get(comp_ref, 0):3d}")

    if unmapped:
        # These are non-test src files with no glob match → probably infra
        excluded_re = [_glob_to_re(p) for p in excluded_globs]
        real_unmapped = [f for f in unmapped if not any(r.match(f) for r in excluded_re)]
        if real_unmapped:
            print(f"\n      [warn] {len(real_unmapped)} files unmapped and not in excluded_globs:")
            for f in real_unmapped[:20]:
                print(f"        - {f}")
            if len(real_unmapped) > 20:
                print(f"        ... and {len(real_unmapped) - 20} more")

    print("\n[done]")


if __name__ == "__main__":
    main()
