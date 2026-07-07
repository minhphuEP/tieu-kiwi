"""Human-readable Vietnamese labels for live agent progress.

The agent loop and heavy tools (currently `ingest_jira_ticket`) emit event
dicts through an `on_step` callback supplied by the caller (Slack, CLI, tests).
This module keeps the display strings in one place so callers only worry about
delivery (Slack chat_update / stdout / logs), not phrasing.

Event shapes:
  {"phase": "thinking"}                              → "🧠 Kiwi đang suy nghĩ…"
  {"phase": "tool_start", "name": <tool>, "args": {...}}
  {"phase": "tool_done",  "name": <tool>}            (rarely shown; use to stop spinners)
  {"phase": "sub", "name": <tool>, "detail": "..."}  → sub-step inside a tool

`label_for()` never raises: an unknown event falls back to a generic label.
"""


_TOOL_LABELS = {
    "search_kb":          "🔍 Đang tìm trong knowledge base…",
    "coverage_gap":       "📋 Đang kiểm tra coverage gaps…",
    "trace":              "🧭 Đang trace{ref} qua graph…",
    "go_no_go":           "🚦 Đang đánh giá GO/NO-GO cho{ref}…",
    "bug_blast_radius":   "💥 Đang tính blast radius cho bug{bug}…",
    "classify_bug":       "🐛 Đang phân loại bug{bug}…",
    "fetch_jira":         "⬇️ Đang gọi Jira lấy{issue}…",
    "ingest_jira_ticket": "📥 Đang import{issue} vào graph…",
    "fetch_confluence":   "📄 Đang tải Confluence page{page}…",
    "gen_testcase":       "✏️ Đang sinh testcase cho{ref}…",
    "gen_test_plan":      "📝 Đang sinh test plan cho{ref}…",
    "gen_critic":         "🧐 Đang review nội dung…",
}


def _slot(args, key):
    # Return " VALUE" if args has key, else "" — lets templates embed refs
    # without ugly spacing when the arg is absent.
    if not isinstance(args, dict):
        return ""
    v = args.get(key)
    return f" {v}" if v else ""


def label_for(ev):
    ev = ev or {}
    phase = ev.get("phase")

    if phase == "thinking":
        return "🧠 Kiwi đang suy nghĩ…"

    if phase == "sub":
        detail = ev.get("detail") or "…"
        return f"  • {detail}"

    if phase in ("tool_start", "tool"):
        name = ev.get("name") or ""
        tmpl = _TOOL_LABELS.get(name)
        if tmpl is None:
            return f"🛠️ Đang chạy tool `{name}`…"
        args = ev.get("args") or {}
        return tmpl.format(
            ref=_slot(args, "requirement_ref"),
            bug=_slot(args, "bug_ref"),
            issue=_slot(args, "issue_key"),
            page=_slot(args, "page_id"),
        )

    return "⏳ Đang xử lý…"
