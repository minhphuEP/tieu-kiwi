"""Seed a sample requirement graph to test go_no_go.

Usage:
    python seed_graph.py

Requirements:
    - DATABASE_URL set (e.g. in .env) pointing at a Postgres instance.
    - The schema applied first: psql "$DATABASE_URL" -f db/schema.sql

This wipes the nodes/edges tables and inserts a small deterministic sample,
so run it only against a development/test database.

Sample (FRONT-3494):
    - AC-1: covered by a passing test  -> ok
    - AC-2: no test                    -> coverage gap
    - AC-3: covered by a failing test  -> failing test
    - BUG-1: open, severity=high, violates AC-1 -> blocking bug

Expected: go_no_go("FRONT-3494") -> NO-GO with all three problem types.
"""

from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db


def reset_graph():
    # Clear the graph so the sample is deterministic (dev/test DB only).
    with db.conn() as c:
        c.execute("DELETE FROM edges")
        c.execute("DELETE FROM nodes")


def seed():
    reset_graph()

    req = db.add_node("Requirement", "FRONT-3494")

    # AC-1: covered by a passing test
    ac1 = db.add_node("AcceptanceCriterion", "AC-1")
    db.add_edge(req, "has", ac1)
    tc1 = db.add_node("TestCase", "TC-1")
    db.add_edge(ac1, "coveredBy", tc1)
    tr1 = db.add_node("TestRun", "TR-1", {"status": "pass"})
    db.add_edge(tc1, "executedBy", tr1)

    # AC-2: no test -> coverage gap
    ac2 = db.add_node("AcceptanceCriterion", "AC-2")
    db.add_edge(req, "has", ac2)

    # AC-3: covered by a failing test
    ac3 = db.add_node("AcceptanceCriterion", "AC-3")
    db.add_edge(req, "has", ac3)
    tc3 = db.add_node("TestCase", "TC-3")
    db.add_edge(ac3, "coveredBy", tc3)
    tr3 = db.add_node("TestRun", "TR-3", {"status": "fail"})
    db.add_edge(tc3, "executedBy", tr3)

    # Open high-severity bug violating AC-1
    bug = db.add_node("Bug", "BUG-1", {"status": "open", "severity": "high"})
    db.add_edge(bug, "violates", ac1)

    return req


def main():
    seed()
    print("Seeded sample graph for FRONT-3494.")
    print("go_no_go('FRONT-3494') =", db.go_no_go("FRONT-3494"))


if __name__ == "__main__":
    main()
