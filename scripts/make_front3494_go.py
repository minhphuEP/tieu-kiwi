"""Make FRONT-3494 evaluate as GO (reversible) — for demoing the go-live buttons.

It does NOT overwrite/seed the whole graph; it only nudges FRONT-3494's data so
go_no_go returns GO:
  - AC-2 gets a covering TestCase TC-2 with a passing TestRun TR-2.
  - AC-3's failing run TR-3 is flipped to status='pass'.
  - The open bug BUG-1 is closed.

Idempotent (safe to run twice). Undo with scripts/reset_front3494.py.

Usage:  python scripts/make_front3494_go.py
"""

from dotenv import load_dotenv; load_dotenv()

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tieukiwi import db

REQ = "FRONT-3494"


def make_go():
    # AC-2 -> coveredBy -> TC-2 -> executedBy -> TR-2 (pass)
    ac2 = db.node_id_for("AC-2", "AcceptanceCriterion")
    if ac2 is not None:
        tc2 = db.upsert_node_by_ref("TestCase", "TC-2", {})
        tr2 = db.upsert_node_by_ref("TestRun", "TR-2", {"status": "pass"})
        db.ensure_edge(ac2, "coveredBy", tc2)
        db.ensure_edge(tc2, "executedBy", tr2)

    # AC-3's run TR-3: fail -> pass
    db.update_node_props("TR-3", "status", "pass", "TestRun")

    # Close the blocking bug
    db.update_node_props("BUG-1", "status", "closed", "Bug")


def main():
    print("before:", db.go_no_go(REQ)["decision"])
    make_go()
    result = db.go_no_go(REQ)
    print("after :", result["decision"])
    if result["decision"] != "GO":
        print("  (still not GO — details:", result, ")")


if __name__ == "__main__":
    main()
