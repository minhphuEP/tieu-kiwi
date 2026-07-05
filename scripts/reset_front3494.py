"""Reset FRONT-3494 back to NO-GO — undoes scripts/make_front3494_go.py.

  - Removes TC-2 / TR-2 (and their edges).
  - Sets AC-3's run TR-3 back to status='fail'.
  - Reopens BUG-1.

Idempotent (safe to run twice).

Usage:  python scripts/reset_front3494.py
"""

from dotenv import load_dotenv; load_dotenv()

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tieukiwi import db

REQ = "FRONT-3494"


def reset():
    # Remove the extra nodes (delete_node_by_ref also removes their edges).
    db.delete_node_by_ref("TR-2", "TestRun")
    db.delete_node_by_ref("TC-2", "TestCase")

    # Restore the original failing run and open bug.
    db.update_node_props("TR-3", "status", "fail", "TestRun")
    db.update_node_props("BUG-1", "status", "open", "Bug")


def main():
    print("before:", db.go_no_go(REQ)["decision"])
    reset()
    print("after :", db.go_no_go(REQ)["decision"])


if __name__ == "__main__":
    main()
