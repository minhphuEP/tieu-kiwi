"""Seed a sample knowledge graph for testing Tieu Kiwi's QE tools.

Usage:
    python scripts/seed/graph.py

Requirements:
    - DATABASE_URL set (see .env.example)
    - Schema applied first: `psql "$DATABASE_URL" -f db/schema.sql`
    - Migration applied: `psql "$DATABASE_URL" -f db/002_migration.sql`

This wipes nodes/edges/users and inserts a small deterministic scenario, so run
only against a development/test database.

Scenario — "Face-recognition login" (Sprint 24), two projects:
    PROJ_AUTH   — the main auth service
    PROJ_NOTIF  — an integration for OTP SMS delivery

Cross-project artifacts on purpose (exercises impact analysis):
    - REQ-101-2 --impacts--> Component[PROJ_NOTIF]
    - Component[PROJ_AUTH] --dependsOn--> Component[PROJ_NOTIF]
    - BUG-501 --affects--> Component[PROJ_NOTIF]

Coverage gap on purpose:
    - AC-101-2 has NO TestCase -> coverage_gap() should surface it.

Expected:
    go_no_go('REQ-101-2') -> NO-GO (coverage gap + open bug)
"""

import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

import psycopg
from tieukiwi import db


# --- helpers ---------------------------------------------------------------

def _add_node(type_, ref, project_id=None, props=None):
    """Wrap db.add_node with project_id (added by migration 002)."""
    with db.conn() as c:
        row = c.execute(
            "INSERT INTO nodes(type, ref, project_id, props_json) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (type_, ref, project_id, psycopg.types.json.Json(props or {})),
        ).fetchone()
        return row[0]


def _add_edge(src_id, rel, dst_id, props=None):
    with db.conn() as c:
        c.execute(
            "INSERT INTO edges(src_id, rel, dst_id, props_json) "
            "VALUES (%s, %s, %s, %s)",
            (src_id, rel, dst_id, psycopg.types.json.Json(props or {})),
        )


def _reset():
    """Clear graph data for a deterministic re-seed (dev/test DB only).

    Does NOT touch the `users` table — users are owned solely by the canonical
    seed (scripts/seed/users_real.py), so graph re-seeds never disturb routing.
    """
    with db.conn() as c:
        c.execute("DELETE FROM edges")
        c.execute("DELETE FROM nodes")


# --- provenance meta --------------------------------------------------------

def _meta_llm(source_file, confidence=0.85, chunk=None):
    return {
        "_meta": {
            "extraction_source": "llm:qwen2.5:7b",
            "confidence": confidence,
            "source_file": source_file,
            "source_chunk": chunk,
            "review_status": "draft",
        }
    }


def _meta_human():
    return {"_meta": {"extraction_source": "human", "confidence": 1.0}}


# --- seed data --------------------------------------------------------------

def seed():
    _reset()

    # Users are seeded separately by scripts/seed/users_real.py (the one source of
    # the users table) — graph.py seeds GRAPH data only.

    # ---- Components (2 projects for cross-project demo) ----
    comp_auth = _add_node("Component", "COMP-AUTH", "PROJ_AUTH",
        {**_meta_human(), "name": "auth-service", "tech_stack": "Golang"})
    comp_face = _add_node("Component", "COMP-FACEAI", "PROJ_AUTH",
        {**_meta_human(), "name": "face-ai-service", "tech_stack": "Python"})
    comp_notif = _add_node("Component", "COMP-NOTIF", "PROJ_NOTIF",
        {**_meta_human(),
         "name": "notification-service",
         "tech_stack": "NodeJS",
         "owner_slack_id": "U07_TL_GIANG"})  # instance override

    # Cross-project dependency
    _add_edge(comp_auth, "dependsOn", comp_notif)

    # ---- Sprint & UserStory ----
    sprint = _add_node("Sprint", "SPR-24", "PROJ_AUTH",
        {**_meta_human(), "name": "Sprint 24",
         "start_date": "2026-06-30", "end_date": "2026-07-14"})
    us = _add_node("UserStory", "US-101", "PROJ_AUTH",
        {**_meta_human(), "title": "Đăng nhập bằng khuôn mặt", "status": "In Code"})
    _add_edge(sprint, "has", us)

    # ---- Requirements (one human, one LLM-extracted to demo _meta) ----
    req1 = _add_node("Requirement", "REQ-101-1", "PROJ_AUTH",
        {**_meta_human(),
         "detail": "Hệ thống nhận diện khuôn mặt trong 3 giây với tỷ lệ >= 98%"})
    req2 = _add_node("Requirement", "REQ-101-2", "PROJ_AUTH",
        {**_meta_llm("requirements/BRD-login.pdf", 0.87, 12),
         "detail": "Fallback OTP qua SMS khi nhận diện khuôn mặt thất bại 3 lần"})
    _add_edge(us, "has", req1)
    _add_edge(us, "has", req2)

    # Requirement -impacts-> Component (cross-project on REQ-101-2)
    _add_edge(req1, "impacts", comp_face)
    _add_edge(req1, "impacts", comp_auth)
    _add_edge(req2, "impacts", comp_auth)
    _add_edge(req2, "impacts", comp_notif)   # cross-project

    # ---- Acceptance Criteria ----
    ac1 = _add_node("AcceptanceCriterion", "AC-101-1", "PROJ_AUTH",
        {**_meta_human(), "desc": "Với ánh sáng đủ, hệ thống nhận diện đúng user trong <= 3s"})
    ac2 = _add_node("AcceptanceCriterion", "AC-101-2", "PROJ_AUTH",
        {**_meta_human(), "desc": "Với ánh sáng yếu, hệ thống hiển thị hướng dẫn tăng sáng"})
    ac3 = _add_node("AcceptanceCriterion", "AC-101-3", "PROJ_AUTH",
        {**_meta_human(), "desc": "Sau 3 lần thất bại, hiển thị nút OTP fallback"})
    ac4 = _add_node("AcceptanceCriterion", "AC-101-4", "PROJ_AUTH",
        {**_meta_human(), "desc": "OTP gửi qua SMS trong 30 giây, hết hạn sau 5 phút"})
    _add_edge(req1, "has", ac1)
    _add_edge(req1, "has", ac2)
    _add_edge(req2, "has", ac3)
    _add_edge(req2, "has", ac4)

    # ---- TestCases (AC-101-2 intentionally uncovered) ----
    tc_a = _add_node("TestCase", "TC-101-A", "PROJ_AUTH",
        {**_meta_human(),
         "title": "Face recog happy path",
         "steps": "1. Mở app 2. Cho camera nhìn 3. Login trong <=3s",
         "expected": "Login OK"})
    tc_b = _add_node("TestCase", "TC-101-C", "PROJ_AUTH",
        {**_meta_human(),
         "title": "OTP fallback after 3 failures",
         "steps": "1. Mở app 2. Che camera 3. Chờ 3 lần fail 4. Bấm OTP",
         "expected": "OTP gửi qua SMS"})
    _add_edge(ac1, "coveredBy", tc_a)
    _add_edge(ac3, "coveredBy", tc_b)
    _add_edge(ac4, "coveredBy", tc_b)
    # AC-101-2 -> no coveredBy edge -> coverage_gap()

    # ---- TestRuns ----
    tr_a = _add_node("TestRun", "RUN-101-A-1", "PROJ_AUTH",
        {**_meta_human(), "status": "pass", "duration_ms": 2100})
    tr_c = _add_node("TestRun", "RUN-101-C-1", "PROJ_AUTH",
        {**_meta_human(), "status": "fail", "duration_ms": 31000,
         "summary": "OTP không tới trong 30s"})
    _add_edge(tc_a, "executedBy", tr_a)
    _add_edge(tc_b, "executedBy", tr_c)

    # ---- Bug (found by failing run, affects cross-project) ----
    bug = _add_node("Bug", "BUG-501", "PROJ_AUTH",
        {**_meta_human(),
         "severity": "high",
         "status": "open",
         "summary": "OTP SMS delayed >30s",
         "origin": "testing",
         "assignee": "U0BF8SHCZ41"})
    _add_edge(tr_c, "finds", bug)
    _add_edge(bug, "affects", comp_auth)
    _add_edge(bug, "affects", comp_notif)   # cross-project
    _add_edge(bug, "violates", ac4)

    # ---- Feedback (about an AC, to demo resolve_owner_slack hop) ----
    fb = _add_node("Feedback", "FB-001", "PROJ_AUTH",
        {"content": "AC-101-3 nên nói rõ 3 lần thất bại LIÊN TIẾP hay tích lũy",
         "created_by": "U0BERHH2F39",
         "status": "pending"})
    _add_edge(fb, "about", ac3)

    return req2


def _print_check():
    with db.conn() as c:
        n_nodes = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        n_edges = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        n_users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    print(f"Seeded: {n_nodes} nodes, {n_edges} edges, {n_users} users.")


def main():
    seed()
    _print_check()
    print("\ngo_no_go('REQ-101-2') =", db.go_no_go("REQ-101-2"))


if __name__ == "__main__":
    main()
