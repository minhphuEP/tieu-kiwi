"""Smoke test for db.feature_blast_radius() + testcase extension in code_impact."""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))
from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db


SEV_TAG = {"high": "[HIGH]  ", "medium": "[MEDIUM]", "low": "[LOW]   "}


def _print_feature(label, r):
    print("=" * 78); print(label); print("=" * 78)
    print(f"  target: {r['target']['ref']} — {r['target'].get('name')}")
    sc = r.get('severity_counts', {})
    print(f"  totals — Components: {sc.get('components')}  "
          f"Reqs: {sc.get('requirements')}  ACs: {sc.get('acs')}  "
          f"TCs: {sc.get('testcases')}")

    print(f"\n  Components at risk ({len(r['affected_components'])}) — target + dependents:")
    for c in r['affected_components']:
        print(f"    {SEV_TAG[c['severity']]} {c['ref']:36s} via={c['via']:10s} depth={c.get('dep_depth')}   {c['name']}")
    if r.get('warning'):
        print(f"\n  WARN: {r['warning']}")
        return

    print(f"\n  Requirements ({len(r['affected_requirements'])}):")
    for req in r['affected_requirements']:
        print(f"    {SEV_TAG[req['severity']]} {req['ref']:12s}  {req.get('title') or '-'}")

    print(f"\n  Testcases to plan ({len(r['affected_testcases'])}):")
    if not r['affected_testcases']:
        print("    (none — testcases haven't been ingested with AC coveredBy links)")
    for tc in r['affected_testcases']:
        print(f"    {SEV_TAG[tc['severity']]} {tc['ref']:24s}  P={tc.get('priority') or '-'}  "
              f"covers={tc.get('covers_acs')}  {(tc.get('title') or '')[:60]}")
    print()


def _print_code(label, r):
    print("=" * 78); print(label); print("=" * 78)
    print(f"  seed:     {r['seed_files']}")
    sc = r.get('severity_counts', {})
    print(f"  totals — Components: {sc.get('components')}  Reqs: {sc.get('requirements')}  "
          f"ACs: {sc.get('acs')}  TCs: {sc.get('testcases')}")
    print(f"\n  affected TCs ({len(r.get('affected_testcases', []))}):")
    if not r.get('affected_testcases'):
        print("    (none — TC not linked to AC via coveredBy)")
    for tc in r.get('affected_testcases', []):
        print(f"    {SEV_TAG[tc['severity']]} {tc['ref']}  P={tc.get('priority') or '-'}  covers={tc.get('covers_acs')}")
    print()


if __name__ == "__main__":
    # Case 1: feature_blast_radius for SCRIPT-ASSIGN
    r = db.feature_blast_radius("COMP-CDM-SCRIPT-ASSIGN", project_id="CDM")
    _print_feature("Case 1: feature_blast_radius(SCRIPT-ASSIGN) — CDM-268 territory", r)

    # Case 2: feature_blast_radius for OFFER-REVIEWER — 1 direct dep down
    r = db.feature_blast_radius("COMP-CDM-OFFER-REVIEWER", project_id="CDM")
    _print_feature("Case 2: feature_blast_radius(OFFER-REVIEWER) — hub feature", r)

    # Case 3: feature_blast_radius for AUTH — many features depend on it
    r = db.feature_blast_radius("COMP-CDM-AUTH", project_id="CDM")
    _print_feature("Case 3: feature_blast_radius(AUTH) — foundation dep", r)

    # Case 4: unknown Component
    r = db.feature_blast_radius("COMP-CDM-NONEXISTENT", project_id="CDM")
    _print_feature("Case 4: unknown component (expect warning)", r)

    # Case 5: code_impact with BE distribution.py — checks testcase extension works
    r = db.code_impact(["reviewer/apps/reviewer/routers/distribution.py"], project_id="CDM")
    _print_code("Case 5: code_impact — expect testcases now (if coveredBy edges exist)", r)
