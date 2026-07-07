"""Demo path B, act 2: import CDM-268 TestCases from Excel + link to ACs.

Story arc for the demo:

    1. `python scripts/seed/cdm_demo.py`
       → seeds CDM-268 base graph (Story, Epic, ACs, Bug, TestRuns, etc.)
       → `go_no_go('CDM-268')` returns NO-GO because AC-CDM-268-3 has no
         TestCase coverage.

    2. `python scripts/seed/cdm_demo_import_tcs.py`   ← THIS SCRIPT
       → runs the generic Excel ingest on the real QE test-suite file,
         adds 8 TestCase nodes to the graph.
       → applies the hand-authored coverage map below to create the missing
         `AcceptanceCriterion -coveredBy-> TestCase` edges.
       → `go_no_go('CDM-268')` now returns GO — story arc closes.

The coverage map is HAND-AUTHORED, not derived from the Excel (the file has no
AC_Refs column). Rework this once `ingest/testcases.py` supports an AC-mapping
input (a JSON sidecar or an extra column). Kept here so demo runs deterministic.

Idempotent: safe to re-run. Excel ingest upserts by (project_id, ref); edge
insertion uses WHERE NOT EXISTS.

Usage:
    python scripts/seed/cdm_demo_import_tcs.py

Requires the base seed to have been run first (needs AC nodes to link to).
"""
import argparse
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db
from scripts.ingest.testcases import ingest as ingest_testcases


PROJECT = "CDM"

EXCEL_RELPATH = _P("kb/CDM/QE/CDM Duplicate-Assign - Test Suite 2026-06-30 (1)_import.xlsx")


def _resolve_excel(user_path):
    """Find the Excel: --file overrides everything; else search repo-root and
    the checked-out main worktree (git worktrees don't share untracked files)."""
    if user_path:
        p = _P(user_path).expanduser().resolve()
        if p.exists():
            return p
        raise SystemExit(f"Excel not found at --file={p}")
    # Try worktree root first, then main checkout upstream of any worktree.
    here = _P(__file__).resolve().parents[2]
    candidates = [here / EXCEL_RELPATH]
    # If we're inside `.claude/worktrees/*/`, add the real repo root too.
    parts = here.parts
    if ".claude" in parts:
        idx = parts.index(".claude")
        main_root = _P(*parts[:idx])
        candidates.append(main_root / EXCEL_RELPATH)
    for c in candidates:
        if c.exists():
            return c
    raise SystemExit(
        "Excel not found. Pass --file=/absolute/path/to/testsuite.xlsx or drop "
        f"the file at kb/CDM/QE/. Looked in:\n" + "\n".join(f"  - {c}" for c in candidates)
    )


# TC (Jira/Excel ref) → ACs it covers.
# Rationale per row (TC titles from the Excel):
#   DupScript_001  visibility per state             → AC-1 duplicate flow
#   DupScript_002  happy flow creates DRAFT script  → AC-1 duplicate + AC-3 DRAFT status
#   DupScript_003  product dropdown search / filter → AC-1 duplicate flow
#   DupScript_004  duplicate validation / archive   → AC-1 duplicate flow
#   AssignCreator_001..003  visibility / manual assign / creator dropdown → AC-2 assign
#   AssignCreator_004  assign validation + Archive blocking + server errors
#                                                    → AC-2 assign + AC-4 archive-block
COVERAGE_MAP = {
    "CDM_DupScript_001":     ["AC-CDM-268-1"],
    "CDM_DupScript_002":     ["AC-CDM-268-1", "AC-CDM-268-3"],
    "CDM_DupScript_003":     ["AC-CDM-268-1"],
    "CDM_DupScript_004":     ["AC-CDM-268-1"],
    "CDM_AssignCreator_001": ["AC-CDM-268-2"],
    "CDM_AssignCreator_002": ["AC-CDM-268-2"],
    "CDM_AssignCreator_003": ["AC-CDM-268-2"],
    "CDM_AssignCreator_004": ["AC-CDM-268-2", "AC-CDM-268-4"],
}


def _find_id(cur, type_, ref, project_id):
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


def link_coverage():
    """Apply COVERAGE_MAP + link imported TCs to executed TestRuns. Idempotent.

    Two edge classes added:
      - AC -coveredBy-> TC        (from COVERAGE_MAP)
      - TC -executedBy-> TR       (from RAN_TESTRUNS — the runs QE has finished)

    Without executedBy edges, `classify_bug` returns `leaked_tc_not_run` for
    any bug whose AC does have a covering TC — misleading, because in
    reality QE ran the full suite on self/dev/beta.
    """
    RAN_TESTRUNS = ("CDM-270", "CDM-271", "CDM-272")  # self, dev, beta (prod pending)
    linked_cov = 0
    linked_exec = 0
    missing_ac = []
    missing_tc = []
    missing_tr = []
    with db.conn() as c:
        cur = c.cursor()
        for tc_ref, ac_refs in COVERAGE_MAP.items():
            tc_id = _find_id(cur, "TestCase", tc_ref, PROJECT)
            if tc_id is None:
                missing_tc.append(tc_ref)
                continue
            # coveredBy: AC → TC
            for ac_ref in ac_refs:
                ac_id = _find_id(cur, "AcceptanceCriterion", ac_ref, PROJECT)
                if ac_id is None:
                    missing_ac.append(ac_ref)
                    continue
                _ensure_edge(cur, ac_id, "coveredBy", tc_id)
                linked_cov += 1
                print(f"  [cov]  {ac_ref} -coveredBy-> {tc_ref}")
            # executedBy: TC → TR (each ran env)
            for tr_ref in RAN_TESTRUNS:
                tr_id = _find_id(cur, "TestRun", tr_ref, PROJECT)
                if tr_id is None:
                    missing_tr.append(tr_ref)
                    continue
                _ensure_edge(cur, tc_id, "executedBy", tr_id)
                linked_exec += 1
    if missing_tc:
        print(f"\n[warn] TestCase nodes not found (Excel ingest failed?): {sorted(set(missing_tc))}")
    if missing_ac:
        print(f"[warn] AC nodes not found (base seed not run?): {sorted(set(missing_ac))}")
    if missing_tr:
        print(f"[warn] TestRun nodes not found: {sorted(set(missing_tr))}")
    return linked_cov, linked_exec


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--file", default=None,
                    help="Path to the CDM-268 Excel testsuite (default: auto-detect).")
    args = ap.parse_args()
    excel = _resolve_excel(args.file)

    print("=" * 70)
    print("BEFORE — go_no_go('CDM-268'):")
    before = db.go_no_go("CDM-268", project_id=PROJECT)
    print(f"  decision      : {before['decision']}")
    print(f"  coverage_gaps : {before['coverage_gaps']}")
    print(f"  next_actions  : {before['next_actions']}")

    print("\n" + "=" * 70)
    print(f"Step 1/2 — ingest Excel: {excel.name}")
    ingested = ingest_testcases(str(excel), PROJECT)
    print(f"  → {len(ingested)} TestCase nodes upserted")

    print("\n" + "=" * 70)
    print("Step 2/2 — link TCs → ACs (coverage) + TCs → TRs (execution):")
    n_cov, n_exec = link_coverage()
    print(f"  → {n_cov} coveredBy edges + {n_exec} executedBy edges ensured")

    print("\n" + "=" * 70)
    print("AFTER — go_no_go('CDM-268'):")
    after = db.go_no_go("CDM-268", project_id=PROJECT)
    print(f"  decision      : {after['decision']}")
    print(f"  coverage_gaps : {after['coverage_gaps']}")
    print(f"  next_actions  : {after['next_actions']}")

    print("\n--- classify_bug for the 5 bugs in CDM-286 table (post-import) ---")
    for i in range(1, 6):
        ref = f"CDM-286-{i}"
        c = db.classify_bug(ref, project_id=PROJECT)
        print(f"  {ref} → {c['category']:22s} | {c['reasoning']}")


if __name__ == "__main__":
    main()
