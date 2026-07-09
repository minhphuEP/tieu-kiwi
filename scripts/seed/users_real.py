"""Canonical users seed — the ONE source of the `users` table.

Resets the table to EXACTLY the 4 real demo users (project CDM, lowercase canonical
roles) so the DB is reproducible identically across machines. Idempotent: it first
deletes the canonical role rows AND any legacy smoke/placeholder rows (fake ids with
underscores, and the old BA/QE_EXECUTOR/TECH_LEAD roles), then inserts the 4.

This is the ONLY place Slack ids are hardcoded. Other seed scripts (graph.py,
cdm_demo.py) no longer touch the users table.

Run (resets users to the 4 real users):
    python scripts/seed/users_real.py
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db

PROJECT = "CDM"

# (slack_id, role, display_name). Lowercase roles match the routing.py constants.
REAL_USERS = [
    ("U0BFE5BV5E0", "delivery_manager", "Delivery Manager"),
    ("U0BERHH2F39", "qe_lead",          "QE Lead"),
    ("U0BEVJVQ14N", "po",               "PO"),
    ("U0BF8SHCZ41", "dev",              "Dev"),
]


def seed():
    with db.conn() as c:
        # 1a) Drop the canonical role rows (case-insensitive) so re-runs are idempotent.
        c.execute(
            "DELETE FROM users WHERE lower(role) IN "
            "('delivery_manager','qe_lead','po','dev')"
        )
        # 1b) Clear legacy smoke/placeholder rows: old roles + fake ids (which all
        #     contain an underscore, e.g. U02_BA_BINH, U_CDM_QE_EXEC). The 4 real ids
        #     have no underscore, so they are never matched here.
        c.execute(
            "DELETE FROM users WHERE role IN ('BA','QE_EXECUTOR','TECH_LEAD') "
            "OR slack_id LIKE %s ESCAPE '\\'",
            ("%\\_%",),
        )
        # 2) Insert exactly the 4 real users.
        for slack_id, role, name in REAL_USERS:
            c.execute(
                "INSERT INTO users (slack_id, display_name, role, project_id) "
                "VALUES (%s, %s, %s, %s)",
                (slack_id, name, role, PROJECT),
            )
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print(f"[ok] users table reset to the {len(REAL_USERS)} real users "
          f"(project={PROJECT}); total rows now = {total}.")


if __name__ == "__main__":
    seed()
