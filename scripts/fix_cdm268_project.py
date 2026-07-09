"""One-off, idempotent data fix: correct CDM-268's nodes from project_id 'CDM_TEAM'
to the canonical 'CDM' (= Jira key prefix, per db.project_id_from_ref).

Scoped to CDM-268 only — the Requirement, its AcceptanceCriteria (via 'has' edges),
and the TestCases covering those ACs (via 'coveredBy'). Other projects are untouched.
Only rows currently at 'CDM_TEAM' are changed, so re-running is a no-op.

Run:  python scripts/fix_cdm268_project.py
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[1]))

from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db

OLD, NEW, REQ = "CDM_TEAM", "CDM", "CDM-268"


def main():
    with db.conn() as c:
        req = c.execute(
            "SELECT id FROM nodes WHERE type='Requirement' AND ref=%s", (REQ,)
        ).fetchone()
        if not req:
            print(f"[skip] Requirement {REQ} not found.")
            return
        req_id = req[0]

        # AcceptanceCriteria of this requirement (via 'has').
        n_ac = c.execute(
            """
            UPDATE nodes SET project_id=%s
            WHERE project_id=%s AND type='AcceptanceCriterion'
              AND id IN (SELECT dst_id FROM edges WHERE src_id=%s AND rel='has')
            """,
            (NEW, OLD, req_id),
        ).rowcount

        # TestCases covering those ACs (via 'coveredBy').
        n_tc = c.execute(
            """
            UPDATE nodes SET project_id=%s
            WHERE project_id=%s AND type='TestCase'
              AND id IN (
                SELECT e2.dst_id FROM edges e2
                WHERE e2.rel='coveredBy' AND e2.src_id IN (
                  SELECT dst_id FROM edges WHERE src_id=%s AND rel='has'
                ))
            """,
            (NEW, OLD, req_id),
        ).rowcount

        # The Requirement node itself.
        n_req = c.execute(
            "UPDATE nodes SET project_id=%s WHERE id=%s AND project_id=%s",
            (NEW, req_id, OLD),
        ).rowcount

    print(f"[ok] {REQ}: moved to project '{NEW}' — Requirement:{n_req} AC:{n_ac} TestCase:{n_tc} "
          f"(rows already '{NEW}' were left unchanged).")


if __name__ == "__main__":
    main()
