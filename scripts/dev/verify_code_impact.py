"""Smoke test for db.code_impact() — runs 3 concrete cases and prints results.

Assumes:
  - Postgres is running with the CDM code graph ingested
    (scripts/seed/cdm_components.py + scripts/ingest/code_graph.py already run).
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db


SEV_TAG = {"high": "[HIGH]  ", "medium": "[MEDIUM]", "low": "[LOW]   "}


def _print(label, result):
    print("=" * 76)
    print(label)
    print("=" * 76)
    print(f"  seed_files:     {result['seed_files']}")
    print(f"  direction:      {result.get('direction')}  depth={result.get('depth_used')}")

    sc = result.get('severity_counts', {})
    print(f"  severity totals — Components: {sc.get('components')}   "
          f"Reqs: {sc.get('requirements')}   ACs: {sc.get('acs')}")

    print(f"  affected CodeUnits: {len(result['affected_code_units'])}")
    if result['affected_code_units']:
        by_depth = {}
        for u in result['affected_code_units']:
            by_depth.setdefault(u.get('depth', 0), []).append(u)
        for d in sorted(by_depth):
            files = sorted({u['file'] for u in by_depth[d] if u['file']})
            print(f"    depth={d} ({len(files)} files): {files[:4]}"
                  + (f" ...+{len(files) - 4}" if len(files) > 4 else ""))

    print(f"  affected Components ({len(result['affected_components'])}) — HIGH → MEDIUM → LOW:")
    for c in result['affected_components']:
        extra = ""
        if c.get('min_depth') is not None:
            extra = f"min_depth={c['min_depth']} touched={c.get('touched_units',0)}"
        print(f"    {SEV_TAG[c['severity']]} {c['ref']:36s} via={c['via']:10s} {extra}")

    print(f"  affected Requirements ({len(result['affected_requirements'])}):")
    for r in result['affected_requirements']:
        print(f"    {SEV_TAG[r['severity']]} {r['ref']:12s}  {r['title'] or '-'}")
        if r.get('impacted_components'):
            print(f"              impacts: {r['impacted_components']}")

    print(f"  affected ACs ({len(result['affected_acs'])}):")
    for a in result['affected_acs']:
        parent = f"  parent={a.get('parent_requirement','-')}" if a.get('parent_requirement') else ""
        print(f"    {SEV_TAG[a['severity']]} {a['ref']}{parent}")

    if 'warning' in result:
        print(f"  WARN: {result['warning']}")
    print()


if __name__ == "__main__":
    # Case 1: single reviewer hook — expect impact on OFFER-REVIEWER page + related
    r = db.code_impact(
        ["frontend/apps/reviewer/src/pages/offers/use-offer-review-actions.ts"],
        project_id="CDM",
    )
    _print("Case 1: change use-offer-review-actions.ts", r)

    # Case 2: assign-modal (SCRIPT-ASSIGN = 1 file, close to CDM-268)
    r = db.code_impact(
        ["frontend/apps/reviewer/src/features/script-assign/assign-modal.tsx"],
        project_id="CDM",
    )
    _print("Case 2: change assign-modal.tsx (CDM-268 territory)", r)

    # Case 3: simulated diff — multiple files across features
    r = db.code_impact(
        [
            "frontend/apps/reviewer/src/features/offers/reviewer-offer-fsm.ts",
            "frontend/apps/creator/src/pages/offers/components/sample-payment-section.tsx",
            "frontend/apps/reviewer/src/features/script-edit/script-content-tab.tsx",
        ],
        project_id="CDM",
    )
    _print("Case 3: multi-file diff across 3 Components", r)

    # Case 4: unknown file — sanity check
    r = db.code_impact(["nonexistent/path.tsx"], project_id="CDM")
    _print("Case 4: unknown file (expect warning + empty results)", r)

    # Case 5: upstream direction (what does this depend on?)
    r = db.code_impact(
        ["frontend/apps/reviewer/src/pages/offers/offer-review-page.tsx"],
        direction="upstream",
        project_id="CDM",
    )
    _print("Case 5: upstream from offer-review-page.tsx (deps audit)", r)
