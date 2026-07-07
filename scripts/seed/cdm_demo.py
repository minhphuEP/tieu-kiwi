"""Seed CDM-268 (real ticket) fixture for demo path B.

Idempotent: uses ON CONFLICT DO UPDATE for nodes/users and WHERE NOT EXISTS
for edges (copies the pattern from scripts/ingest/bugs.py). Does NOT wipe
existing data — coexists safely with `scripts/seed/graph.py` (Face scenario).

Real data sourced from https://crossian.atlassian.net/browse/CDM-268:

    Epic  CDM-275  — New CDM Phase 2.2 — Creator Testing Flow & Update lifecycle
    Story CDM-268  — Reviewer_Duplicate & assign Creator for a Script (Beta Ready)
    Confluence page 2541551769 anchor "15.-Assign-new-creator-for-a-booking-script-phase-2--ready"

    Subtasks:
        CDM-269  Create test cases    (Done)   — workflow item, skipped
        CDM-270  Self test            (Done)   → TestRun env=self  status=pass
        CDM-271  Test on dev          (Done)   → TestRun env=dev   status=pass
        CDM-272  Test on beta         (Done)   → TestRun env=beta  status=pass
        CDM-273  Test on prod         (To Do)  → TestRun env=prod  status=pending
        CDM-286  [Bug] batch          (Done)   → NOT a single bug! Description
                                                  is a markdown table with 5 rows,
                                                  one bug per row. Seeded as
                                                  CDM-286-1 .. CDM-286-5. Real
                                                  prod ingest will parse the table
                                                  from the ADF description.

Hand-authored (not from Jira/Confluence — placeholder content for demo):

    4 AcceptanceCriteria drafted from PRD section 15
      (AC-1 duplicate, AC-2 assign, AC-3 DRAFT status, AC-4 archive-block)
    2 TestCases (1 lead_approved, 1 qe_reviewed) — demo review-state fixtures
    1 Component `reviewer-portal`
    4 users for project CDM (real display names from Jira reporter/assignee;
      slack_ids are placeholders — replace before wiring the Slack layer.)

Expected agent-tool output after seed:

    trace('CDM-268')             → full Requirement → AC → TC → TestRun → Bug path
    coverage_gap(CDM)       → [AC-CDM-268-3, AC-CDM-268-4] (uncovered)
    classify_bug('CDM-286-1..3') → caught_by_test  (find_by=Testcase, found by TR CDM-270)
    classify_bug('CDM-286-4')    → leaked_tc_missing  (find_by=Lack, violates uncovered AC-4)
    classify_bug('CDM-286-5')    → caught_by_test
    go_no_go('CDM-268')          → NO-GO
                                   next_actions: ['Write a testcase for AC-CDM-268-3',
                                                  'Write a testcase for AC-CDM-268-4']

Usage:
    python scripts/seed/cdm_demo.py

Requirements:
    - DATABASE_URL set (see .env.example)
    - Schema + migrations 002/003/004 applied
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

import psycopg
from tieukiwi import db


PROJECT = "CDM"
BRD_REF = "CFL-2541551769"
BRD_ANCHOR = "15.-Assign-new-creator-for-a-booking-script-phase-2--ready"
BRD_URL = (
    "https://crossian.atlassian.net/wiki/spaces/tech/pages/2541551769/"
    "PRD+-+CDM+portal+for+reviewers#" + BRD_ANCHOR
)


# --- idempotent helpers (mirrors scripts/ingest/bugs.py) -------------------

def _upsert_node(cur, type_, ref, project_id, props):
    """INSERT or UPDATE via the (project_id, ref) partial unique index (003)."""
    cur.execute(
        """
        INSERT INTO nodes (type, ref, project_id, props_json)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (project_id, ref) WHERE ref IS NOT NULL DO UPDATE
          SET props_json = nodes.props_json || EXCLUDED.props_json
        RETURNING id
        """,
        (type_, ref, project_id, psycopg.types.json.Json(props)),
    )
    return cur.fetchone()[0]


def _ensure_edge(cur, src_id, rel, dst_id, props=None):
    """Insert an edge only if the same (src, rel, dst) triple doesn't already exist."""
    cur.execute(
        """
        INSERT INTO edges (src_id, rel, dst_id, props_json)
        SELECT %s, %s, %s, %s
        WHERE NOT EXISTS (
          SELECT 1 FROM edges WHERE src_id=%s AND rel=%s AND dst_id=%s
        )
        """,
        (src_id, rel, dst_id, psycopg.types.json.Json(props or {}),
         src_id, rel, dst_id),
    )


def _upsert_user(cur, slack_id, display_name, role, project_id,
                 jira_account_id=None, email=None):
    cur.execute(
        """
        INSERT INTO users (slack_id, jira_account_id, email, display_name, role, project_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (slack_id) DO UPDATE
          SET display_name    = EXCLUDED.display_name,
              role            = EXCLUDED.role,
              project_id      = EXCLUDED.project_id,
              jira_account_id = COALESCE(EXCLUDED.jira_account_id, users.jira_account_id),
              email           = COALESCE(EXCLUDED.email,           users.email)
        """,
        (slack_id, jira_account_id, email, display_name, role, project_id),
    )


def _meta_jira(source_ref, confidence=1.0):
    """Provenance for data pulled from Jira REST (structured, high confidence)."""
    return {"_meta": {
        "extraction_source": "jira-rest",
        "confidence": confidence,
        "source_file": f"https://crossian.atlassian.net/browse/{source_ref}",
        "review_status": "verified",
    }}


def _meta_confluence():
    return {"_meta": {
        "extraction_source": "confluence-rest",
        "confidence": 1.0,
        "source_file": BRD_URL,
        "review_status": "verified",
    }}


def _meta_human():
    return {"_meta": {"extraction_source": "human", "confidence": 1.0,
                      "review_status": "verified"}}


# --- seed data --------------------------------------------------------------

def seed():
    with db.conn() as c:
        cur = c.cursor()

        # ---- Users (routing target for CDM) -----------------------
        # Real names from Jira; slack_ids are placeholders. Replace with real
        # slack_ids before wiring the Slack layer (Layer B).
        _upsert_user(cur, "U_CDM_PO_OANH",   "Oanh Kieu Thi Nguyen (PO)",
                     "PO",          PROJECT, email="oanh.ktnguyen@crossian.com")
        _upsert_user(cur, "U_CDM_DEV_TAN",   "Tan Duc Nguyen (Dev)",
                     "DEV",         PROJECT, email="tan.ducnguyen@crossian.com")
        _upsert_user(cur, "U_CDM_QE_LEAD",   "QE Lead CDM (placeholder)",
                     "QE_LEAD",     PROJECT)
        _upsert_user(cur, "U_CDM_QE_EXEC",   "QE Executor CDM (placeholder)",
                     "QE_EXECUTOR", PROJECT)

        # ---- Component (single one for CDM portal) --------------------
        comp = _upsert_node(cur, "Component", "COMP-CDM-REVIEWER", PROJECT, {
            **_meta_human(),
            "name": "reviewer-portal",
            "tech_stack": "TBD",
        })

        # ---- Epic → UserStory node (CDM-275) --------------------------
        epic = _upsert_node(cur, "UserStory", "CDM-275", PROJECT, {
            **_meta_jira("CDM-275"),
            "title": "New CDM - Phase 2.2 - Creator Testing Flow & Update offer lifecycle",
            "status": "To Do",
            "jira_issuetype": "Epic",
        })

        # ---- Story CDM-268 → Requirement node -------------------------
        req = _upsert_node(cur, "Requirement", "CDM-268", PROJECT, {
            **_meta_jira("CDM-268"),
            "title": "Reviewer_Duplicate & assign Creator for a Script",
            "status": "Beta Ready",
            "jira_issuetype": "Story",
            "jira_parent_ref": "CDM-275",
            "assignee": "U_CDM_DEV_TAN",
            "reporter": "U_CDM_PO_OANH",
            "confluence_page_id": "2541551769",
            "confluence_url": BRD_URL,
            "confluence_section_anchor": BRD_ANCHOR,
            "tms_test_run_url": "https://tms.selless.org/test-runs/1c4aa7d5-a1a0-475f-9196-f8217c5b7be6",
        })
        _ensure_edge(cur, epic, "has", req)
        _ensure_edge(cur, req, "impacts", comp)

        # ---- BRD (Confluence PRD section) ------------------------------
        brd = _upsert_node(cur, "BRD", BRD_REF, PROJECT, {
            **_meta_confluence(),
            "title": "PRD - CDM portal for reviewers",
            "space": "tech",
            "page_id": "2541551769",
            "section_anchor": BRD_ANCHOR,
            "section_title": "15. Assign new creator for a booking script (phase 2 - ready)",
            "url": BRD_URL,
            "version": "2.0",
            "last_modified": "2026-06-25",
        })
        _ensure_edge(cur, req, "derivedFrom", brd,
                     props={"section_anchor": BRD_ANCHOR})

        # ---- Acceptance Criteria (hand-authored from PRD §15) ----------
        # AC-1..2 will be covered by the 8 Excel TCs (see cdm_demo_import_tcs.py).
        # AC-3 (DRAFT status) is uncovered until Excel TCs are imported.
        # AC-4 (Archive-block on assign) surfaces bug 4 which is find_by=Lack —
        # keeps the classify_bug=leaked_tc_* demo path alive.
        ac1 = _upsert_node(cur, "AcceptanceCriterion", "AC-CDM-268-1", PROJECT, {
            **_meta_human(),
            "desc": "Reviewer có thể duplicate một booking script và giữ nguyên toàn bộ dữ liệu gốc.",
        })
        ac2 = _upsert_node(cur, "AcceptanceCriterion", "AC-CDM-268-2", PROJECT, {
            **_meta_human(),
            "desc": "Reviewer có thể assign một Creator mới cho script vừa duplicate.",
        })
        ac3 = _upsert_node(cur, "AcceptanceCriterion", "AC-CDM-268-3", PROJECT, {
            **_meta_human(),
            "desc": "Sau khi duplicate, script mới có status = DRAFT, script gốc không thay đổi.",
        })
        ac4 = _upsert_node(cur, "AcceptanceCriterion", "AC-CDM-268-4", PROJECT, {
            **_meta_human(),
            "desc": "Không cho assign creator mới khi product đã Archive; hiển thị lỗi rõ ràng.",
        })
        for ac in (ac1, ac2, ac3, ac4):
            _ensure_edge(cur, req, "has", ac)

        # ---- TestCases (2 hand-authored, AC-3 left uncovered) ---------
        tc_a = _upsert_node(cur, "TestCase", "TC-CDM-268-A", PROJECT, {
            **_meta_human(),
            "title": "Happy path: duplicate + assign new creator",
            "steps": "1. Mở script gốc  2. Bấm Duplicate  3. Chọn Creator mới  4. Save",
            "expected": "Script mới xuất hiện trong list với creator đã chọn",
            "review_status": "lead_approved",
            "reviewed_by_qe": "U_CDM_QE_EXEC",
            "reviewed_by_qe_lead": "U_CDM_QE_LEAD",
        })
        tc_b = _upsert_node(cur, "TestCase", "TC-CDM-268-B", PROJECT, {
            **_meta_human(),
            "title": "Regression: script gốc không đổi sau khi duplicate",
            "steps": "1. Ghi lại data script gốc  2. Duplicate  3. So sánh data gốc trước/sau",
            "expected": "Script gốc bit-for-bit không đổi",
            "review_status": "qe_reviewed",
            "reviewed_by_qe": "U_CDM_QE_EXEC",
        })
        _ensure_edge(cur, ac1, "coveredBy", tc_a)
        _ensure_edge(cur, ac2, "coveredBy", tc_a)
        _ensure_edge(cur, ac1, "coveredBy", tc_b)
        # AC-CDM-268-3 → no coveredBy → coverage_gap()

        # ---- TestRuns (4 subtasks in Jira: self / dev / beta / prod) ---
        # ref matches the Jira subtask key so `trace()` can show env progression.
        tr_self = _upsert_node(cur, "TestRun", "CDM-270", PROJECT, {
            **_meta_jira("CDM-270"),
            "environment": "self",
            "status": "pass",
            "summary": "Self test follow test cases",
        })
        tr_dev = _upsert_node(cur, "TestRun", "CDM-271", PROJECT, {
            **_meta_jira("CDM-271"),
            "environment": "dev", "status": "pass",
            "summary": "Test on dev",
        })
        tr_beta = _upsert_node(cur, "TestRun", "CDM-272", PROJECT, {
            **_meta_jira("CDM-272"),
            "environment": "beta", "status": "pass",
            "summary": "Test on beta",
        })
        tr_prod = _upsert_node(cur, "TestRun", "CDM-273", PROJECT, {
            **_meta_jira("CDM-273"),
            "environment": "prod", "status": "pending",
            "summary": "Test on prod (not started)",
        })
        # Both TCs get executed in every env that ran (Jira doesn't track that
        # granularity — this is a demo simplification).
        for tr in (tr_self, tr_dev, tr_beta):
            _ensure_edge(cur, tc_a, "executedBy", tr)
            _ensure_edge(cur, tc_b, "executedBy", tr)

        # ---- Bugs (from CDM-286 subtask description table) ------------
        # The [Bug] subtask CDM-286 contains a *markdown table* in its
        # description — one ROW = one bug. Real prod ingest will parse this
        # table; here we hard-code the 5 rows for demo. Ref pattern is
        # <subtask_ref>-<row_index>.
        #
        # Field mapping from the Jira table columns:
        #   `Priority`  → props.severity   (High → high, Medium → medium)
        #   `Status`/🟢 → props.status     (🟢 = done; blank = open)
        #   `Find by`   → props.find_by    ("Testcase" → caught via TR CDM-270;
        #                                    "Lack"    → discovered exploratory,
        #                                                no `finds` edge → classify=leaked_*)
        # Ontology edges per row:
        #   All bugs -affects-> COMP-CDM-REVIEWER
        #   find_by=Testcase → TR CDM-270 -finds-> Bug  (classify=caught_by_test)
        #   find_by=Lack     → no finds edge; violates uncovered AC (leaked_tc_missing)
        BUGS = [
            {
                "idx": 1, "severity": "high", "status": "done",  "find_by": "Testcase",
                "summary": "Không show product trong dropdown khi bấm duplicate",
                "steps":    "Click on duplicate → dropdown → close popup → duplicate again",
                "actual":   "Không hiển thị list product trong dropdown",
                "expected": "Hiển thị product trong dropdown",
                "violates": ac1,       # duplicate flow
            },
            {
                "idx": 2, "severity": "high", "status": "done",  "find_by": "Testcase",
                "summary": "Hiển thị list creator đã assign offer trong popup assign",
                "steps":    "Assign creator A → về script tab → bấm assign",
                "actual":   "Vẫn hiển thị creator A trong ds assign",
                "expected": "Chỉ hiển thị creator chưa được assign",
                "violates": ac2,       # assign creator
            },
            {
                "idx": 3, "severity": "high", "status": "done",  "find_by": "Testcase",
                "summary": "Không show popup confirm khi assign cho creator",
                "steps":    "Assign creator A → bấm submit",
                "actual":   "Submit thành công, end process",
                "expected": "Hiển thị popup confirmation trước khi kết thúc",
                "violates": ac2,       # assign creator
            },
            {
                "idx": 4, "severity": "medium", "status": "open", "find_by": "Lack",
                "summary": "Vẫn assign được cho creator khi product đã archive",
                "steps":    "Tạo offer product A → archive product → assign script cho creator",
                "actual":   "Assign thành công (đáng lẽ phải chặn)",
                "expected": "Chặn không assign được và báo lỗi product đã archive",
                "violates": ac4,       # archive validation — uncovered until Excel TCs import
            },
            {
                "idx": 5, "severity": "medium", "status": "open", "find_by": "Testcase",
                "summary": "Show list model creator cho script type Studio khi assign",
                "steps":    "Tạo 1 script type=studio → assign script vừa tạo",
                "actual":   "Hiển thị list creator type=model",
                "expected": "Hiển thị list creator đúng theo type của script",
                "violates": ac2,       # assign creator (wrong list filter)
            },
        ]
        for b in BUGS:
            bug_ref = f"CDM-286-{b['idx']}"
            bug_id = _upsert_node(cur, "Bug", bug_ref, PROJECT, {
                **_meta_jira("CDM-286"),
                "summary": b["summary"],
                "severity": b["severity"],
                "status": b["status"],
                "find_by": b["find_by"],
                "origin": "testing",
                "assignee": "U_CDM_DEV_TAN",
                "reporter": "U_CDM_PO_OANH",   # bugs reported by team's QE
                "jira_container_ref": "CDM-286",  # subtask that holds the table
                "jira_parent_ref":    "CDM-268",  # story
                "description": {
                    "steps":    b["steps"],
                    "actual":   b["actual"],
                    "expected": b["expected"],
                },
            })
            _ensure_edge(cur, bug_id, "affects", comp)
            _ensure_edge(cur, bug_id, "violates", b["violates"])
            if b["find_by"] == "Testcase":
                # QE self-test caught it → classify_bug = caught_by_test
                _ensure_edge(cur, tr_self, "finds", bug_id)
            # find_by=Lack: no `finds` edge → classify_bug depends on AC coverage.


def _print_check():
    with db.conn() as c:
        cur = c.cursor()
        n = cur.execute(
            "SELECT type, COUNT(*) FROM nodes WHERE project_id=%s GROUP BY type ORDER BY type",
            (PROJECT,),
        ).fetchall()
        n_edges = cur.execute(
            """
            SELECT COUNT(*) FROM edges e
            JOIN nodes s ON s.id=e.src_id
            WHERE s.project_id=%s
            """,
            (PROJECT,),
        ).fetchone()[0]
        n_users = cur.execute(
            "SELECT COUNT(*) FROM users WHERE project_id=%s", (PROJECT,)
        ).fetchone()[0]
    print(f"\n[{PROJECT}] seeded:")
    for type_, count in n:
        print(f"  {type_:<24s} {count}")
    print(f"  edges (from this project): {n_edges}")
    print(f"  users (this project):      {n_users}")


def main():
    seed()
    _print_check()

    print("\n--- coverage_gap(CDM) ---")
    print(db.coverage_gap(project_id=PROJECT))

    print("\n--- classify_bug for the 5 bugs in CDM-286 table ---")
    for i in range(1, 6):
        ref = f"CDM-286-{i}"
        c = db.classify_bug(ref, project_id=PROJECT)
        print(f"  {ref} → {c['category']:22s} | {c['reasoning']}")

    print("\n--- go_no_go('CDM-268') ---")
    print(db.go_no_go("CDM-268", project_id=PROJECT))


if __name__ == "__main__":
    main()
