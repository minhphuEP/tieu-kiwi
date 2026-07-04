"""Wipe the graph (edges + nodes + users) for a clean re-ingest.

Use in development only. Preserves kb_rules, promotion_queue, thread_state.

Usage:
    python scripts/seed/reset.py            # asks for confirmation
    python scripts/seed/reset.py --yes      # skip confirmation
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

import argparse
from tieukiwi import db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="Skip confirmation prompt.")
    args = ap.parse_args()

    with db.conn() as c:
        n_edges = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        n_nodes = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        n_users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print(f"Current: {n_nodes} nodes, {n_edges} edges, {n_users} users.")

    if not args.yes:
        resp = input("Delete ALL edges + nodes + users? [y/N] ").strip().lower()
        if resp != "y":
            print("Aborted.")
            return

    with db.conn() as c:
        c.execute("DELETE FROM edges")
        c.execute("DELETE FROM nodes")
        c.execute("DELETE FROM users")
    print("[ok] Graph wiped.")


if __name__ == "__main__":
    main()
