"""Manual verification for db.requirement_with_acs / db.testcases_for_requirement.
Requires: Postgres up, migrations applied, scripts/seed/cdm_demo.py already run.
Run: python scripts/dev/verify_requirement_with_acs.py
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db

prd = db.requirement_with_acs("CDM-268", project_id="CDM_TEAM")
assert prd["found"] is True, prd
assert prd["ref"] == "CDM-268"
assert prd["title"] == "Reviewer_Duplicate & assign Creator for a Script", prd["title"]
ac_refs = sorted(ac["ref"] for ac in prd["acs"])
assert ac_refs == ["AC-CDM-268-1", "AC-CDM-268-2", "AC-CDM-268-3", "AC-CDM-268-4"], ac_refs
assert all(ac["desc"] for ac in prd["acs"]), prd["acs"]

missing = db.requirement_with_acs("CDM-NOPE-999", project_id="CDM_TEAM")
assert missing["found"] is False, missing

existing = db.testcases_for_requirement("CDM-268", project_id="CDM_TEAM")
by_ref = {tc["ref"]: tc for tc in existing}
assert "TC-CDM-268-A" in by_ref, by_ref.keys()
assert sorted(by_ref["TC-CDM-268-A"]["ac_refs"]) == ["AC-CDM-268-1", "AC-CDM-268-2"], by_ref["TC-CDM-268-A"]

print("OK")
