"""Smoke test for code_impact after BE graph ingested — proves FE↔BE join
through Component."""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))
from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db
from scripts.dev.verify_code_impact import _print   # reuse formatter


if __name__ == "__main__":
    # BE case 1: reviewer assign-modal BE-router
    r = db.code_impact(
        ["reviewer/apps/reviewer/routers/distribution.py"],
        project_id="CDM",
    )
    _print("BE Case 1: change distribution.py (BE assign endpoint)", r)

    # BE case 2: shared offers domain model → wide impact
    r = db.code_impact(
        ["packages/samx-core/samx_core/apps/offers/models/offer.py"],
        project_id="CDM",
    )
    _print("BE Case 2: change core offer model", r)

    # Cross FE+BE diff: one FE file + one BE file for CDM-268 flow
    r = db.code_impact(
        [
            "frontend/apps/reviewer/src/features/script-assign/assign-modal.tsx",
            "reviewer/apps/reviewer/routers/distribution.py",
        ],
        project_id="CDM",
    )
    _print("Cross-stack: FE assign-modal + BE distribution router (CDM-268)", r)
