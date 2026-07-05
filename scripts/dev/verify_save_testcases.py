"""Manual verification for db.save_testcases. Requires Postgres up + migrations
applied + scripts/seed/cdm_demo.py already run (for AC-CDM-268-3 to exist).
Cleans up the test node it creates so it's safe to re-run.
Run: python scripts/dev/verify_save_testcases.py
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

from tieukiwi import db

TEST_REF = "TC-VERIFY-SAVE-001"
db.delete_node_by_ref(TEST_REF, type_="TestCase")  # start clean

draft = [{
    "ref": TEST_REF,
    "ac_refs": ["AC-CDM-268-3"],
    "title": "[TC-VERIFY-SAVE-001] Verify save_testcases upserts and links",
    "priority": "Medium",
    "precondition": "",
    "steps": [{"description": "do X", "expected": "see Y"}],
    "data_variants": [],
}]

ids_1 = db.save_testcases("CDM-268", draft, approved_by="U_TEST", project_id="CDM_TEAM")
assert len(ids_1) == 1

props = db.get_node_props(TEST_REF, type_="TestCase")
assert props["title"] == draft[0]["title"], props
assert props["_meta"]["review_status"] == "verified", props["_meta"]
assert props["_meta"]["approved_by"] == "U_TEST", props["_meta"]

ac_id = db.node_id_for("AC-CDM-268-3", type_="AcceptanceCriterion")
tc_id = db.node_id_for(TEST_REF, type_="TestCase")
gap_before = [ref for _, ref in db.coverage_gap(project_id="CDM_TEAM")]
assert "AC-CDM-268-3" not in gap_before, gap_before  # now covered

# Re-run with a changed title -> should update in place, not duplicate.
draft[0]["title"] = "[TC-VERIFY-SAVE-001] Updated title"
ids_2 = db.save_testcases("CDM-268", draft, approved_by="U_TEST", project_id="CDM_TEAM")
assert ids_2 == ids_1, (ids_2, ids_1)
props_2 = db.get_node_props(TEST_REF, type_="TestCase")
assert props_2["title"] == "[TC-VERIFY-SAVE-001] Updated title"

db.delete_node_by_ref(TEST_REF, type_="TestCase")  # cleanup
print("OK")
