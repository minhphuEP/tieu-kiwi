"""Seed the `users` directory (routing target).

Idempotent — ON CONFLICT (slack_id) DO NOTHING. Safe to re-run.

The 7 seed users below are placeholders for the demo. Replace / extend with
real Slack IDs from the workspace before wiring Layer B (Slack bot).
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db


SEED_USERS = [
    # slack_id,        display_name,       role,          project_id,   jira_account_id,  email
    # ---- Project-scoped users (demo: PROJ_AUTH + PROJ_NOTIF) ----
    ("U01_PO_ANH",     "Anh (PO)",         "PO",          "PROJ_AUTH",  "557058:po-anh",   "po.anh@example.com"),
    ("U02_BA_BINH",    "Binh (BA)",        "BA",          "PROJ_AUTH",  "557058:ba-binh",  "ba.binh@example.com"),
    ("U03_QE_CUONG",   "Cuong (QE Lead)",  "QE_LEAD",     "PROJ_AUTH",  "557058:qe-cuong", "qe.cuong@example.com"),
    ("U04_QE_DUNG",    "Dung (QE Exec)",   "QE_EXECUTOR", "PROJ_AUTH",  "557058:qe-dung",  "qe.dung@example.com"),
    ("U05_DEV_EM",     "Em (Dev)",         "DEV",         "PROJ_AUTH",  "557058:dev-em",   "dev.em@example.com"),
    ("U06_TL_FONG",    "Fong (Tech Lead)", "TECH_LEAD",   "PROJ_AUTH",  "557058:tl-fong",  "tl.fong@example.com"),
    ("U07_TL_GIANG",   "Giang (TL Notif)", "TECH_LEAD",   "PROJ_NOTIF", "557058:tl-giang", "tl.giang@example.com"),
    # ---- Global fallback (project_id=NULL) so routing works for any project ----
    ("U10_PO_GLOBAL",  "Global PO",        "PO",          None,         None,              None),
    ("U11_BA_GLOBAL",  "Global BA",        "BA",          None,         None,              None),
    ("U12_QL_GLOBAL",  "Global QE Lead",   "QE_LEAD",     None,         None,              None),
    ("U13_QE_GLOBAL",  "Global QE Exec",   "QE_EXECUTOR", None,         None,              None),
    ("U14_DEV_GLOBAL", "Global Dev",       "DEV",         None,         None,              None),
    ("U15_TL_GLOBAL",  "Global Tech Lead", "TECH_LEAD",   None,         None,              None),
]


def upsert_user(slack_id, display_name, role, project_id=None, jira_account_id=None, email=None):
    """Insert or update ONE user, keyed by slack_id. Handy for adding yourself to a role
    while testing — re-running with a different role flips it (unlike the bulk seed which
    is DO NOTHING). Parameterized SQL.

    Example (add yourself as delivery_manager, global):
        python scripts/seed/users.py --add U0BEZ40TFAM delivery_manager
        python scripts/seed/users.py --add U0BEZ40TFAM qe_lead PROJ_AUTH "Me (QE Lead)"
    """
    with db.conn() as c:
        c.execute(
            """
            INSERT INTO users (slack_id, jira_account_id, email, display_name, role, project_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (slack_id) DO UPDATE SET
              display_name    = EXCLUDED.display_name,
              role            = EXCLUDED.role,
              project_id      = EXCLUDED.project_id,
              jira_account_id = COALESCE(EXCLUDED.jira_account_id, users.jira_account_id),
              email           = COALESCE(EXCLUDED.email, users.email)
            """,
            (slack_id, jira_account_id, email, display_name, role, project_id),
        )
    return slack_id


def main():
    import argparse
    p = argparse.ArgumentParser(description="Seed / add users for ask-routing.")
    p.add_argument(
        "--add", nargs="+", metavar="ARG",
        help="Upsert one user: --add <slack_id> <role> [project_id] [display_name]",
    )
    args = p.parse_args()

    if args.add:
        slack_id = args.add[0]
        role = args.add[1]
        project = args.add[2] if len(args.add) > 2 else None
        name = args.add[3] if len(args.add) > 3 else slack_id
        upsert_user(slack_id, name, role, project)
        print(f"[ok] upserted {slack_id} as role='{role}' project={project}")
        return

    with db.conn() as c:
        for slack_id, name, role, project, jira, email in SEED_USERS:
            c.execute(
                """
                INSERT INTO users (slack_id, jira_account_id, email, display_name, role, project_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (slack_id) DO NOTHING
                """,
                (slack_id, jira, email, name, role, project),
            )
        n = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print(f"[ok] users table: {n} rows.")


if __name__ == "__main__":
    main()
