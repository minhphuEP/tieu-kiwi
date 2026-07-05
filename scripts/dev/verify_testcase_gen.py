"""Manual verification for testcase_gen.generate_draft/refine_draft/finalize_and_save,
using a stub LLM (no API calls) and an isolated fixture (own project_id/Requirement/AC,
created and torn down by this script — does not touch shared demo data like CDM_TEAM).
Requires: Postgres up, migrations applied, VOYAGEAI_API_KEY set in .env (rag.search is
called for real inside _fetch_kb_context; empty results are fine, but the key must exist).
Run: python scripts/dev/verify_testcase_gen.py
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

import json

from tieukiwi import db, testcase_gen

PROJECT = "ZTMP_GENTEST"
REQ_REF = "REQ-ZTMP-GENTEST-001"
AC_REF = "AC-ZTMP-GENTEST-001"
TC_REF = "TC-ZTMP-GENTEST-001"


def _cleanup():
    db.delete_node_by_ref(TC_REF, type_="TestCase")
    db.delete_node_by_ref(AC_REF, type_="AcceptanceCriterion")
    db.delete_node_by_ref(REQ_REF, type_="Requirement")


_cleanup()  # start clean in case a prior run left state behind

req_id = db.add_node("Requirement", ref=REQ_REF, props={
    "title": "Isolated fixture requirement for testcase_gen verification",
    "detail": "Synthetic requirement used only by scripts/dev/verify_testcase_gen.py.",
})
ac_id = db.add_node("AcceptanceCriterion", ref=AC_REF, props={
    "desc": "Synthetic AC used only by this verification script.",
})
db.add_edge(req_id, "has", ac_id)
# add_node() has no project_id kwarg, so set it directly — requirement_with_acs(project_id=PROJECT)
# and testcases_for_requirement(project_id=PROJECT) both filter on nodes.project_id.
with db.conn() as c:
    c.execute("UPDATE nodes SET project_id=%s WHERE id IN (%s, %s)", (PROJECT, req_id, ac_id))


def stub_llm_generate(prompt, system=None):
    return {
        "testcases": [{
            "ref": TC_REF,
            "ac_refs": [AC_REF],
            "title": f"[{TC_REF}] Stub-generated testcase",
            "priority": "Medium",
            "precondition": "",
            "steps": [{"description": "do X", "expected": "see Y"}],
            "data_variants": [],
        }],
        "summary": f"Stub draft for {AC_REF}.",
    }


draft = testcase_gen.generate_draft(REQ_REF, project_id=PROJECT, llm_fn=stub_llm_generate)
assert draft["version"] == 1
assert draft["testcases"][0]["ref"] == TC_REF
assert draft["requirement_ref"] == REQ_REF


def stub_llm_refine(prompt, system=None):
    return {
        "testcases": [{
            "ref": TC_REF,
            "ac_refs": [AC_REF],
            "title": f"[{TC_REF}] Refined per reviewer comment",
            "priority": "High",
            "precondition": "",
            "steps": [{"description": "do X", "expected": "see Y"}],
            "data_variants": [],
        }],
        "summary": "Bumped priority to High per reviewer comment.",
    }


refined = testcase_gen.refine_draft(draft, "please bump priority to High", llm_fn=stub_llm_refine)
assert refined["version"] == 2
assert refined["testcases"][0]["priority"] == "High"


def stub_llm_should_not_be_called(prompt, system=None):
    raise AssertionError("LLM should not be called for a full-replacement comment")


replacement_comment = json.dumps(refined["testcases"])
refined_2 = testcase_gen.refine_draft(refined, replacement_comment, llm_fn=stub_llm_should_not_be_called)
assert refined_2["version"] == 3
assert refined_2["testcases"] == refined["testcases"]

node_ids = testcase_gen.finalize_and_save(refined_2, approved_by="U_TEST")
assert len(node_ids) == 1
props = db.get_node_props(TC_REF, type_="TestCase")
assert props["title"] == f"[{TC_REF}] Refined per reviewer comment"
assert props["_meta"]["approved_by"] == "U_TEST"

_cleanup()
print("OK")
