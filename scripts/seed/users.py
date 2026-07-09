"""Deprecated shim — the canonical users seed is scripts/seed/users_real.py.

Kept so the old `python scripts/seed/users.py` command still resets the users
table to exactly the 4 real users. There is NO fake/placeholder user list here
anymore; all user seeding lives in users_real.py (one source of truth).
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parent))

from users_real import seed  # noqa: E402  (same directory)

if __name__ == "__main__":
    seed()
