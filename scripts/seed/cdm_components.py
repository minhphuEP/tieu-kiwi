"""Seed CDM feature-level Components + Component-to-Component `dependsOn` edges.

Fills the architectural knowledge gap: `scripts/ingest/requirements.py` (LLM)
creates Components from PRD prose with `Requirement -impacts-> Component`
edges, but does NOT create Component-to-Component `dependsOn` edges — that is
architectural knowledge, not stated in PRDs. This script is where that
knowledge lives, version-controlled.

Model: Component = a BUSINESS FEATURE / user-facing capability, not a service
or code module. `dependsOn` edges are hand-authored (agreed with PO / tech
lead), never extracted from code imports.

Idempotent: upsert Components; edges deduped by (src, rel, dst).

Usage:
    python scripts/seed/cdm_components.py

Requirements:
    - DATABASE_URL set (see .env.example)
    - Schema + migrations 002/003/004 applied

Fill in the COMPONENTS and DEPENDS_ON lists below, then run.
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

import psycopg
from tieukiwi import db


# NOTE: graph (Postgres) uses "CDM_TEAM" for the CDM project; Chroma KB uses
# "CDM". Different tenant keys — leave as-is unless you're consolidating.
PROJECT = "CDM"


# ---------------------------------------------------------------------------
# FILL THIS IN — one entry per feature/capability in CDM.
# Ref convention: COMP-CDM-<UPPERCASE-KEBAB-SLUG>.
# `name` is the human-readable label used in reports / Slack messages.
# ---------------------------------------------------------------------------
COMPONENTS = [
    # (ref,                              name)
    # ("COMP-CDM-SAMPLE-PREPARING",      "Sample Preparing"),
    # ("COMP-CDM-SAMPLE-SHIPPING",       "Sample Shipping"),
    # ("COMP-CDM-OFFER-MGMT",            "Offer Management"),
    # ("COMP-CDM-PAYMENT",               "Payment Processing"),
    # ("COMP-CDM-CREATOR-ONBOARDING",    "Creator Onboarding"),
    # ("COMP-CDM-SCRIPT-MGMT",           "Script Management"),
    # TODO: replace placeholders with the real feature list (from PRDs + PO discussion).
]


# ---------------------------------------------------------------------------
# FILL THIS IN — one entry per feature-to-feature dependency.
# Tuple meaning: src DEPENDS ON dst  →  src cannot work correctly if dst is broken.
# ---------------------------------------------------------------------------
DEPENDS_ON = [
    # (src_ref,                          dst_ref)
    # ("COMP-CDM-SAMPLE-SHIPPING",       "COMP-CDM-SAMPLE-PREPARING"),  # can't ship what isn't prepared
    # ("COMP-CDM-SAMPLE-SHIPPING",       "COMP-CDM-PAYMENT"),           # ship flow calls fee calc
    # ("COMP-CDM-OFFER-MGMT",            "COMP-CDM-CREATOR-ONBOARDING"),
    # TODO: fill in from PO / tech lead conversation. Not auto-extractable.
]


# --- helpers (mirror scripts/seed/cdm_demo.py) -----------------------------

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


def _ensure_edge(cur, src_id, rel, dst_id, props=None):
    cur.execute(
        """
        INSERT INTO edges (src_id, rel, dst_id, props_json)
        SELECT %s, %s, %s, %s
        WHERE NOT EXISTS (
          SELECT 1 FROM edges WHERE src_id=%s AND rel=%s AND dst_id=%s
        )
        """,
        (src_id, rel, dst_id, psycopg.types.json.Json(props or {}),
         src_id, rel, dst_id),
    )


def _find_component_id(cur, ref, project_id):
    row = cur.execute(
        "SELECT id FROM nodes WHERE type='Component' AND ref=%s AND project_id=%s",
        (ref, project_id),
    ).fetchone()
    return row[0] if row else None


def _meta_human_seed():
    return {"_meta": {
        "extraction_source": "human-seed",
        "confidence": 1.0,
        "review_status": "verified",
        "source_file": "scripts/seed/cdm_components.py",
    }}


# --- seed ------------------------------------------------------------------

def seed():
    if not COMPONENTS:
        raise SystemExit(
            "COMPONENTS list is empty. Edit scripts/seed/cdm_components.py "
            "and fill in the real CDM feature list before running."
        )

    with db.conn() as c:
        cur = c.cursor()

        # 1) Upsert Components
        ids = {}
        for ref, name in COMPONENTS:
            ids[ref] = _upsert_node(cur, "Component", ref, PROJECT, {
                **_meta_human_seed(),
                "name": name,
            })

        # 2) Insert dependsOn edges. Look up refs that may already exist from
        #    prior ingest (LLM extracted from PRDs).
        skipped = []
        for src_ref, dst_ref in DEPENDS_ON:
            src_id = ids.get(src_ref) or _find_component_id(cur, src_ref, PROJECT)
            dst_id = ids.get(dst_ref) or _find_component_id(cur, dst_ref, PROJECT)
            if src_id is None:
                skipped.append((src_ref, dst_ref, f"src Component not found: {src_ref}"))
                continue
            if dst_id is None:
                skipped.append((src_ref, dst_ref, f"dst Component not found: {dst_ref}"))
                continue
            _ensure_edge(cur, src_id, "dependsOn", dst_id)

        if skipped:
            print("\n[warn] Skipped edges (missing Components):")
            for src, dst, reason in skipped:
                print(f"  {src} → {dst}  ({reason})")


def _print_check():
    with db.conn() as c:
        cur = c.cursor()
        comps = cur.execute(
            "SELECT ref, props_json->>'name' FROM nodes "
            "WHERE type='Component' AND project_id=%s ORDER BY ref",
            (PROJECT,),
        ).fetchall()
        deps = cur.execute(
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
        print(f"  {ref:40s} {name or '-'}")

    print(f"\n[{PROJECT}] Component dependsOn ({len(deps)}):")
    for src, dst in deps:
        print(f"  {src:40s} → {dst}")


def main():
    seed()
    _print_check()


if __name__ == "__main__":
    main()
