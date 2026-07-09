"""Dedupe Requirement nodes that share a `ref` (idempotent).

Root cause it repairs: fetch_jira (before the upsert fix) could create a second
Requirement node for the same ref, so the AC 'has' edges pointed at one node while
the go_no_go/coverage flow read the other (0 ACs). This consolidates each ref to a
single Requirement node:

  - KEEP the node with the most 'has' edges (i.e. the one linked to ACs); tie-break
    by lowest id.
  - Repoint every edge (src_id / dst_id) from the duplicates onto the kept node,
    then remove any exact-duplicate edges the repoint produced.
  - Merge props_json from duplicates into the kept node (duplicates overlay, so the
    freshest Jira metadata wins; kept-only keys like _meta survive).
  - Delete the duplicate nodes (no orphaned edges — all repointed first).

Safe to re-run: once a ref has a single Requirement node, it is skipped.

Run:  python scripts/dedupe_requirements.py          # all duplicated refs
      python scripts/dedupe_requirements.py CDM-268  # just this ref
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[1]))

from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db


def _has_edge_count(c, node_id):
    return c.execute(
        "SELECT count(*) FROM edges WHERE src_id=%s AND rel='has'", (node_id,)
    ).fetchone()[0]


def dedupe_ref(c, ref):
    rows = c.execute(
        "SELECT id, props_json FROM nodes WHERE type='Requirement' AND ref=%s ORDER BY id",
        (ref,),
    ).fetchall()
    if len(rows) <= 1:
        return 0  # nothing to merge

    # Pick the keeper: most 'has' edges (AC links), tie-break lowest id.
    ranked = sorted(rows, key=lambda r: (-_has_edge_count(c, r[0]), r[0]))
    keeper_id, keeper_props = ranked[0]
    keeper_props = dict(keeper_props or {})
    dups = ranked[1:]

    for dup_id, dup_props in dups:
        # Merge props (duplicate overlays keeper -> fresh Jira metadata wins).
        keeper_props.update(dup_props or {})
        # Repoint all edges off the duplicate onto the keeper.
        c.execute("UPDATE edges SET src_id=%s WHERE src_id=%s", (keeper_id, dup_id))
        c.execute("UPDATE edges SET dst_id=%s WHERE dst_id=%s", (keeper_id, dup_id))

    # Remove exact-duplicate edges created by repointing (keep one per src,rel,dst).
    c.execute(
        """
        DELETE FROM edges a USING edges b
        WHERE a.id > b.id
          AND a.src_id=b.src_id AND a.rel=b.rel AND a.dst_id=b.dst_id
        """
    )
    # Write merged props onto the keeper, then delete the now-detached duplicates.
    c.execute(
        "UPDATE nodes SET props_json=%s WHERE id=%s",
        (db.psycopg.types.json.Json(keeper_props), keeper_id),
    )
    for dup_id, _ in dups:
        c.execute("DELETE FROM nodes WHERE id=%s", (dup_id,))

    print(f"[ok] {ref}: kept node {keeper_id}, removed {len(dups)} duplicate(s).")
    return len(dups)


def main(refs=None):
    with db.conn() as c:
        if refs:
            targets = list(refs)
        else:
            targets = [
                r[0] for r in c.execute(
                    "SELECT ref FROM nodes WHERE type='Requirement' AND ref IS NOT NULL "
                    "GROUP BY ref HAVING count(*) > 1"
                ).fetchall()
            ]
        if not targets:
            print("[ok] no duplicated Requirement refs found.")
            return
        removed = sum(dedupe_ref(c, ref) for ref in targets)
    print(f"[done] removed {removed} duplicate Requirement node(s) across {len(targets)} ref(s).")


if __name__ == "__main__":
    main(sys.argv[1:] or None)
