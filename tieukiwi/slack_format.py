"""Canonical Slack formatter for Tieu Kiwi.

There is exactly ONE public entry point — `to_slack(answer)` — used by every Slack
reply (slash command, app_mention, interim + final messages). It parses the agent's
free-form Markdown answer into a structured report and re-renders it in the single
canonical layout below. Non-report text (free chat / errors) falls back to a plain
mrkdwn cleanup.

Canonical layout:

    *FRONT-3494 — Optimize Memory Storefront*

    :label: *Loại:* Story
    :pushpin: *Trạng thái:* :white_check_mark: Done
    :zap: *Priority:* Medium
    :bust_in_silhouette: *Assignee (Owner):* An Quoc Tran
    :memo: *Reporter:* Truong Nguyen Chi
    :rocket: *Fix Version:* Front - Release 26S7.1 - W1
    :dart: *Story Points:* 15.5

    :test_tube: *Tình trạng Test Case*
    Story này có *3 Acceptance Criteria*, nhưng coverage *chưa đầy đủ*:
    *Test coverage (3 acceptance criteria):*
    :white_check_mark: Entry point: TC-1 PASS
    :x: Assign new creator modal: chưa có testcase
    :red_circle: Confirm view: TC-3 FAIL

    :warning: *Cần lưu ý*
    1. ...
    2. ...

    > :bulb: ...

Slack mrkdwn only: bold = single *pair*; never "**", "|", "---", "##", "#:".
"""

import re

# ---------------------------------------------------------------- shared regex/const

_SEPARATOR_CELL = re.compile(r":?-{1,}:?")
_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_MULTI_STAR = re.compile(r"\*{2,}")
_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
_AC_LIKE = re.compile(r"(?i)\bAC[\s\-]?\d+")
_TC = re.compile(r"(?i)\bTC[\s\-]?\d+")
_TR = re.compile(r"(?i)\bTR[\s\-]?\d+")
_EMOJI = re.compile(r":[a-z0-9_+\-]+:")

_EMPTY = {"", "-", "—", "–", "n/a", "na", "none", "null", "chưa", "chưa có", "không", "không có"}

# Label alias -> canonical field.
_ALIASES = {
    "type":         {"type", "loại", "issue type", "issuetype"},
    "status":       {"status", "trạng thái"},
    "priority":     {"priority", "mức ưu tiên", "độ ưu tiên", "ưu tiên"},
    "assignee":     {"assignee", "owner", "assignee (owner)", "người phụ trách",
                     "phụ trách", "người thực hiện"},
    "reporter":     {"reporter", "người báo cáo", "báo cáo"},
    "fix_version":  {"fix version", "fix versions", "fixversion", "fix version(s)", "phiên bản"},
    "story_points": {"story points", "story point", "điểm", "sp"},
    "summary":      {"summary", "tiêu đề", "title", "tóm tắt", "tên"},
}

# Render order: (field, emoji, canonical label).
_INFO_ORDER = [
    ("type",         ":label:",              "Loại"),
    ("status",       ":pushpin:",            "Trạng thái"),
    ("priority",     ":zap:",                "Priority"),
    ("assignee",     ":bust_in_silhouette:", "Assignee (Owner)"),
    ("reporter",     ":memo:",               "Reporter"),
    ("fix_version",  ":rocket:",             "Fix Version"),
    ("story_points", ":dart:",               "Story Points"),
]

_SECTION_HEADER = re.compile(r"^(:[a-z0-9_]+:\s*)?\*[^*]+\*\s*$")


# ---------------------------------------------------------------- small helpers

def _strip_all(s):
    # Remove emoji shortcodes / asterisks / backticks / bullets for label matching.
    s = _EMOJI.sub("", s or "")
    s = s.replace("*", "").replace("`", "")
    return s.strip().strip("-•> ").strip()


def _clean_val(v):
    v = re.sub(r"\*\*(.+?)\*\*", r"*\1*", v or "")
    return v.replace("|", " ").strip().strip("*").strip()


def _empty(v):
    return (v or "").strip().lower() in _EMPTY


def _field_for(raw):
    s = _strip_all(raw).lower().rstrip(":").strip()
    if not s:
        return None
    for field, aliases in _ALIASES.items():
        if s in aliases:
            return field
    for field, aliases in _ALIASES.items():
        if any(s.startswith(a) for a in aliases):
            return field
    return None


# ---------------------------------------------------------------- table parsing

def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_sep(cells):
    nonempty = [c for c in cells if c != ""]
    return bool(nonempty) and all(_SEPARATOR_CELL.fullmatch(c) for c in nonempty)


def _table_runs(lines):
    runs, i = [], 0
    while i < len(lines):
        if "|" in lines[i] and lines[i].strip():
            run = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                run.append(lines[i])
                i += 1
            runs.append(run)
        else:
            i += 1
    return runs


def _table_rows(run):
    rows = [_cells(l) for l in run]
    sep = next((k for k, c in enumerate(rows) if _is_sep(c)), None)
    header = rows[sep - 1] if (sep is not None and sep > 0) else None
    skip = set()
    if sep is not None:
        skip.add(sep)
        if header is not None:
            skip.add(sep - 1)
    data = [r for k, r in enumerate(rows) if k not in skip and not _is_sep(r)]
    return data, header


def _cell(row, idx):
    return row[idx].strip() if (idx is not None and idx < len(row)) else ""


def _coverage_from_table(header, rows):
    if not header:
        return []
    h = [_strip_all(x).lower() for x in header]

    def find(preds):
        for i, name in enumerate(h):
            if any(p in name for p in preds):
                return i
        return None

    ac_i = find(["ac", "acceptance", "criteri"])
    if ac_i is None:
        return []
    tc_i = find(["test case", "testcase", "tc"])
    tr_i = find(["test run", "testrun", "run", "tr"])
    st_i = find(["status", "result", "kết quả"])
    cov_i = find(["cover", "có test", "covered"])

    out = []
    for r in rows:
        ref_raw = _strip_all(_cell(r, ac_i))
        if not _AC_LIKE.search(ref_raw):
            continue
        ref = ref_raw.upper().replace(" ", "-")
        tc, tr, stt, cov = _cell(r, tc_i), _cell(r, tr_i), _cell(r, st_i), _cell(r, cov_i)
        out.append(_make_ac(ref, tc, tr, stt, cov))
    return out


def _make_ac(ref, tc, tr, status_text, cov_text=""):
    sl = (status_text or "").lower()
    result = "PASS" if "pass" in sl else ("FAIL" if "fail" in sl else None)
    covered = None
    if cov_text:
        cl = cov_text.lower()
        if any(x in cl for x in ("có", "yes", "true")) and not any(x in cl for x in ("chưa", "không")):
            covered = True
        elif any(x in cl for x in ("chưa", "no", "không", "false")):
            covered = False
    if covered is None:
        covered = (bool(tc) and not _empty(tc)) or result in ("PASS", "FAIL")
    return {
        "ref": ref,
        "covered": covered,
        "tc": tc if not _empty(tc) else None,
        "tr": tr if not _empty(tr) else None,
        "result": result,
    }


def _parse_ac_lines(lines):
    out = []
    for ln in lines:
        if "|" in ln:
            continue
        m = re.search(r"(?i)\b(AC[\s\-]?\d+)\b", ln)
        if not m:
            continue
        low = ln.lower()
        if not any(k in low for k in ("pass", "fail", "coverage gap", "chưa có",
                                      "no test", "không có", "covered", "có (")):
            continue
        ref = m.group(1).upper().replace(" ", "-")
        tc = _TC.search(ln)
        tr = _TR.search(ln)
        out.append(_make_ac(ref,
                            tc.group(0).upper().replace(" ", "-") if tc else "",
                            tr.group(0).upper().replace(" ", "-") if tr else "",
                            ln))
    return out


# ---------------------------------------------------------------- line parsing

def _labeled_line(ln):
    s = re.sub(r"^([-*•>\s]|:[a-z0-9_]+:)+", "", ln.strip()).strip()
    if ":" not in s:
        return None
    label, _, value = s.partition(":")
    label = _strip_all(label)
    if not label or len(label) > 40:
        return None
    return label, value.strip()


def _first_key(text):
    m = _KEY.search(text)
    return m.group(1) if m else None


def _extract_title(lines, key, summary):
    if key and summary:
        return f"{key} — {summary}"
    if key:
        for ln in lines:
            m = _HEADING.match(ln)
            if m and key in m.group(1):
                return m.group(1).strip()
        for ln in lines:
            if key in ln and "—" in ln:
                m = re.search(re.escape(key) + r"\s*—\s*([^*#>|:]+)", ln)
                if m and m.group(1).strip():
                    return f"{key} — {m.group(1).strip()}"
        return key
    return summary


def _note_item(raw):
    r = re.sub(r"^#:\s*\d+\s*[•·|\-]*\s*", "", raw.strip())
    r = re.sub(r"^\s*(\d+[.)]|[-*•])\s*", "", r).strip()
    if re.search(r"vấn đề\s*:", r, re.I):
        prob_m = re.search(r"vấn đề\s*:\s*(.+?)(?:[•·|]|$|(?=\bmức độ\b))", r, re.I)
        sev_m = re.search(r"mức độ\s*:\s*(.+?)(?:[•·|]|$)", r, re.I)
        prob = prob_m.group(1).strip(" •·|") if prob_m else r
        sev = sev_m.group(1).strip(" •·|") if sev_m else None
        return f"{prob} (mức độ: {sev})" if sev else prob
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", r).strip()


def _extract_notes(lines):
    start = None
    for i, ln in enumerate(lines):
        core = _strip_all(ln).lower()
        if re.fullmatch(r"(cần lưu ý|lưu ý|khuyến nghị|notes?|issues?|recommendations?)", core):
            start = i + 1
            break

    if start is not None:
        notes = []
        for ln in lines[start:]:
            raw = ln.strip()
            if not raw:
                if notes:
                    break
                continue
            if raw.startswith(">") or _SECTION_HEADER.match(raw) or raw.startswith("#"):
                break
            item = _note_item(raw)
            if item:
                notes.append(item)
        return notes

    # No explicit section: only pick up numbered / "#:" / "Vấn đề" style items.
    notes = []
    for ln in lines:
        raw = ln.strip()
        if re.match(r"^\s*(\d+[.)]|#:)", raw) or re.search(r"vấn đề\s*:", raw, re.I):
            item = _note_item(raw)
            if item:
                notes.append(item)
    return notes


def _extract_suggestion(lines):
    for ln in lines:
        s = ln.strip()
        if s.startswith(">"):
            s = re.sub(r"^>\s*", "", s)
            s = re.sub(r"^:bulb:\s*", "", s)
            return re.sub(r"\*\*(.+?)\*\*", r"*\1*", s).strip()
    return None


# ---------------------------------------------------------------- parse -> report

def _parse(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    info, acs = {}, []
    for run in _table_runs(lines):
        rows, header = _table_rows(run)
        if not rows:
            continue
        ncols = max(len(r) for r in rows)
        if ncols <= 2:
            for r in rows:
                if len(r) >= 2:
                    f = _field_for(r[0])
                    if f and f not in info:
                        info[f] = _clean_val(r[1])
        else:
            acs.extend(_coverage_from_table(header, rows))

    for ln in lines:
        if "|" in ln:
            continue
        kv = _labeled_line(ln)
        if kv:
            f = _field_for(kv[0])
            if f and f not in info:
                info[f] = _clean_val(kv[1])

    if not acs:
        acs = _parse_ac_lines(lines)

    key = _first_key(text)
    summary = info.pop("summary", None)
    title = _extract_title(lines, key, summary)
    notes = _extract_notes(lines)
    suggestion = _extract_suggestion(lines)

    if not (info or acs):
        return None
    return {"title": title, "key": key, "info": info,
            "acs": acs, "notes": notes, "suggestion": suggestion}


# ---------------------------------------------------------------- render (canonical)

def _status_value_icon(status):
    s = (status or "").lower()
    if any(w in s for w in ("done", "closed", "resolved", "pass", "complete", "hoàn thành")):
        return ":white_check_mark:"
    if any(w in s for w in ("progress", "doing", "review", "đang")):
        return ":hourglass_flowing_sand:"
    if any(w in s for w in ("todo", "open", "backlog", "chưa")):
        return ":white_circle:"
    return ""


def _ac_line(ac):
    # Prefer the AC's description over its opaque hash-ref (AC-CDM-268-0063f2af);
    # QE reads titles, not hashes. Fall back to ref if desc is missing (legacy
    # nodes / import from Excel without a title).
    label = (ac.get("desc") or "").strip() or ac.get("ref") or "?"
    label = label.replace("\n", " ").strip()
    if len(label) > 120:
        label = label[:117] + "…"
    result = ac.get("result")
    if result == "PASS":
        icon, tail = ":white_check_mark:", f"{ac.get('tc') or 'TC'} PASS"
    elif result == "FAIL":
        icon, tail = ":red_circle:", f"{ac.get('tc') or 'TC'} FAIL"
    elif ac.get("covered"):
        tc = ac.get("tc") or "TC"
        icon, tail = ":large_blue_circle:", f"{tc} chưa chạy"
    else:
        icon, tail = ":x:", "chưa có testcase"
    return f"{icon} {label}: {tail}"


# Section builders — the single source of truth for the canonical blocks. Both the
# story-report path (to_slack) and the go-live path (render_golive) compose these.

def _header_lines(report):
    return [f"*{report['title']}*"] if report.get("title") else []


def _info_lines(report):
    info = report.get("info") or {}
    lines = []
    for field, emoji, label in _INFO_ORDER:
        val = info.get(field)
        if not val:
            continue
        if field == "status":
            val = f"{_status_value_icon(val)} {val}".strip()
        lines.append(f"{emoji} *{label}:* {val}")
    return lines


def _coverage_lines_block(report):
    acs = report.get("acs") or []
    if not acs:
        return []
    n = len(acs)
    adequate = all(a.get("covered") and a.get("result") == "PASS" for a in acs)
    coverage_word = "*đầy đủ*" if adequate else "*chưa đầy đủ*"
    joiner = "và" if adequate else "nhưng"
    lines = [
        ":test_tube: *Tình trạng Test Case*",
        f"Story này có *{n} Acceptance Criteria*, {joiner} coverage {coverage_word}:",
        f"*Test coverage ({n} acceptance criteria):*",
    ]
    lines.extend(_ac_line(a) for a in acs)
    return lines


def _notes_lines(report):
    notes = report.get("notes") or []
    if not notes:
        return []
    lines = [":warning: *Cần lưu ý*"]
    lines.extend(f"{i}. {note}" for i, note in enumerate(notes, 1))
    return lines


def _suggestion_lines(report):
    s = report.get("suggestion")
    return [f"> :bulb: {s}"] if s else []


def _join_sections(sections):
    # Join section line-lists with a blank line between non-empty sections.
    out = []
    for sec in sections:
        if not sec:
            continue
        if out:
            out.append("")
        out.extend(sec)
    return _MULTI_STAR.sub("*", "\n".join(out))


def _render(report):
    return _join_sections([
        _header_lines(report),
        _info_lines(report),
        _coverage_lines_block(report),
        _notes_lines(report),
        _suggestion_lines(report),
    ])


# Public alias: render a structured report to canonical Slack mrkdwn.
render_report = _render


# ---------------------------------------------------------------- plain fallback

def _plain(text):
    out = []
    for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _HR.match(ln) or ("|" in ln and ln.strip().startswith("|")):
            continue
        ln = re.sub(r"\*\*(.+?)\*\*", r"*\1*", ln)
        ln = re.sub(r"__(.+?)__", r"*\1*", ln)
        m = _HEADING.match(ln)
        if m:
            ln = f"*{m.group(1).strip()}*" if m.group(1).strip() else ""
        out.append(ln)
    return _MULTI_STAR.sub("*", "\n".join(out))


# ---------------------------------------------------------------- public entry point

def to_slack(answer):
    """Render any agent answer into the single canonical Slack mrkdwn format."""
    if not answer:
        return answer
    report = _parse(answer)
    if report and (report.get("info") or report.get("acs")):
        return _render(report)
    return _plain(answer)


# ---------------------------------------------------------------- go-live rendering

def report_from_graph(key, props, trace_result):
    """Build a canonical report dict from a Requirement's graph node props (as stored by
    ingest_jira_ticket) + the output of db.trace(). Pure transformation — no DB access here."""
    props = props or {}
    fix = props.get("fix_versions")
    fix_str = ", ".join(fix) if isinstance(fix, list) else (fix or None)
    sp = props.get("story_points")
    info = {
        "type": props.get("issuetype"),
        "status": props.get("status"),
        "priority": props.get("priority"),
        "assignee": props.get("assignee"),
        "reporter": props.get("reporter"),
        "fix_version": fix_str,
        "story_points": str(sp) if sp is not None else None,
    }
    info = {k: v for k, v in info.items() if v}

    summary = props.get("summary")
    title = f"{key} — {summary}" if summary else key

    acs = []
    for ac in (trace_result or {}).get("acceptance_criteria", []):
        tcs = ac.get("testcases") or []
        tc = tcs[0]["ref"] if tcs else None
        tr = None
        result = None
        if tcs and tcs[0].get("runs"):
            run = tcs[0]["runs"][0]
            tr = run.get("ref")
            st = (run.get("status") or "").lower()
            result = "PASS" if st == "pass" else ("FAIL" if st == "fail" else None)
        acs.append({"ref": ac.get("ref"), "desc": ac.get("desc"),
                    "covered": ac.get("covered"),
                    "tc": tc, "tr": tr, "result": result})

    return {"title": title, "key": key, "info": info, "acs": acs,
            "notes": [], "suggestion": None}


def _golive_decision_lines(res):
    decision = res.get("decision")
    req = res.get("requirement")
    icon = ":large_green_circle:" if decision == "GO" else ":red_circle:"
    # Split gap counts so QE sees "3 uncovered · 2 awaiting review" instead of
    # a single number that hides how much is truly unwritten vs just unreviewed.
    # Falls back gracefully for callers still returning the pre-split shape.
    uncovered = res.get("coverage_uncovered")
    awaiting = res.get("coverage_awaiting_review") or []
    if uncovered is None:
        uncovered = res.get("coverage_gaps") or []
    fails = res.get("failing_tests") or []
    bugs = res.get("open_bugs") or []

    gap_bits = [f"{len(uncovered)} coverage gap(s)"]
    if awaiting:
        gap_bits.append(f"{len(awaiting)} awaiting QE review")
    return [
        f"{icon} *Go/No-Go — {req}: {decision}*",
        "",
        f":test_tube: {' · '.join(gap_bits)} · "
        f":x: {len(fails)} failing test(s) · "
        f":lady_beetle: {len(bugs)} open bug(s)",
    ]


def render_golive(report, res):
    """Compose the go-live message text: the SAME header + ticket-info + coverage blocks
    as the story report, then the go/no-go decision, then (only on non-GO) the next-actions.
    The Approve/Reject buttons are Block Kit structure added by the Slack layer."""
    sections = [
        _header_lines(report),
        _info_lines(report),
        _coverage_lines_block(report),
        _golive_decision_lines(res),
    ]
    if res.get("decision") != "GO":
        actions = res.get("next_actions") or []
        if actions:
            note_lines = [":warning: *Cần lưu ý*"]
            note_lines.extend(f"{i}. {a}" for i, a in enumerate(actions, 1))
            sections.append(note_lines)
    return _join_sections(sections)


# ---------------------------------------------------------------- self-test

# Two DIFFERENT source answers (a full story report vs a go_no_go result), in two
# different Markdown variants, must render to the SAME canonical structure.
_SAMPLE_STORY = """## FRONT-3494 — Optimize Memory Storefront

| Field | Value |
|-------|-------|
| Loại | Story |
| Trạng thái | Done |
| Priority | Medium |
| Assignee (Owner) | An Quoc Tran |
| Reporter | Truong Nguyen Chi |
| Fix Version | Front - Release 26S7.1 - W1 |
| Story Points | 15.5 |

### Tình trạng Test Case

| AC | Test Case | Test Run | Status |
|----|-----------|----------|--------|
| AC-1 | TC-1 | TR-1 | PASS |
| AC-2 | — | — | coverage gap |
| AC-3 | TC-3 | TR-3 | FAIL |

**Cần lưu ý**
1. **AC-2** chưa được viết test case → **coverage gap** cần được bổ sung.
2. **AC-3 đang FAIL** (TR-3 failed) → cần kiểm tra lại và tạo bug report nếu cần.
3. Dù story đã ở trạng thái **Done**, vẫn còn rủi ro chất lượng do AC-2 thiếu test và AC-3 fail.

> :bulb: Bạn có muốn tôi **gen test case cho AC-2** hoặc đánh giá **Go/No-Go** cho FRONT-3494 không?
"""

_SAMPLE_GONOGO = """Go/No-Go cho FRONT-3494 — Optimize Memory Storefront: **NO-GO**

- **Loại:** Story
- **Trạng thái:** Done
- **Priority:** Medium
- **Assignee (Owner):** An Quoc Tran
- **Reporter:** Truong Nguyen Chi
- **Fix Version:** Front - Release 26S7.1 - W1
- **Story Points:** 15.5

Test coverage:
| AC | Test Case | Test Run | Status |
|----|-----------|----------|--------|
| AC-1 | TC-1 | TR-1 | PASS |
| AC-2 |  |  | coverage gap |
| AC-3 | TC-3 | TR-3 | FAIL |

#: 1 • Vấn đề: AC-2 chưa có test case (coverage gap) • Mức độ: cao
#: 2 • Vấn đề: AC-3 đang FAIL (TR-3) • Mức độ: cao
#: 3 • Vấn đề: Story Done nhưng còn rủi ro chất lượng • Mức độ: trung bình

> :bulb: Bạn có muốn tôi gen test case cho AC-2 hoặc đánh giá Go/No-Go cho FRONT-3494 không?
"""


def _selftest():
    a = to_slack(_SAMPLE_STORY)
    b = to_slack(_SAMPLE_GONOGO)
    return a + "\n\n========================================\n\n" + b


_GL_INFO = {
    "type": "Story", "status": "Done", "priority": "Medium",
    "assignee": "An Quoc Tran", "reporter": "Truong Nguyen Chi",
    "fix_version": "Front - Release 26S7.1 - W1", "story_points": "15.5",
}
_GL_TITLE = "FRONT-3494 — Optimize Memory Storefront"


def _golive_selftest():
    report_go = {
        "title": _GL_TITLE, "key": "FRONT-3494", "info": dict(_GL_INFO), "notes": [], "suggestion": None,
        "acs": [
            {"ref": "AC-1", "covered": True, "tc": "TC-1", "tr": "TR-1", "result": "PASS"},
            {"ref": "AC-2", "covered": True, "tc": "TC-2", "tr": "TR-2", "result": "PASS"},
            {"ref": "AC-3", "covered": True, "tc": "TC-3", "tr": "TR-3", "result": "PASS"},
        ],
    }
    res_go = {"requirement": "FRONT-3494", "decision": "GO", "coverage_gaps": [],
              "failing_tests": [], "open_bugs": [], "next_actions": []}

    report_nogo = {
        "title": _GL_TITLE, "key": "FRONT-3494", "info": dict(_GL_INFO), "notes": [], "suggestion": None,
        "acs": [
            {"ref": "AC-1", "covered": True, "tc": "TC-1", "tr": "TR-1", "result": "PASS"},
            {"ref": "AC-2", "covered": False, "tc": None, "tr": None, "result": None},
            {"ref": "AC-3", "covered": True, "tc": "TC-3", "tr": "TR-3", "result": "FAIL"},
        ],
    }
    res_nogo = {
        "requirement": "FRONT-3494", "decision": "NO-GO",
        "coverage_gaps": ["AC-2"],
        "failing_tests": [{"testrun": "TR-3", "testcase": "TC-3"}],
        "open_bugs": [{"bug": "BUG-1", "severity": "high"}],
        "next_actions": ["Write a testcase for AC-2",
                         "Fix failing testcase TC-3 (run TR-3)",
                         "Close bug BUG-1 (high)"],
    }
    return render_golive(report_go, res_go), render_golive(report_nogo, res_nogo)


def render_ac_list(acs):
    """List the Acceptance Criteria a draft covers, as Slack mrkdwn — ref +
    description only, one line each. No testcase mapping (that level of
    detail lives in the exported Excel file only): the Slack message stays a
    quick scan of "are all the ACs I care about here", not a report.

    Builds mrkdwn directly instead of routing through to_slack(): see the
    landmine documented on to_slack's _parse_ac_lines() — it rewrites any
    text containing an AC-like ref plus a trigger keyword into a fabricated
    go-live coverage report. AC refs are unavoidable here, so skip to_slack
    entirely rather than fight the trigger keyword list.
    """
    lines = [f"*Acceptance Criteria ({len(acs)}):*"]
    lines.extend(
        f"• *{ac['ref']}* — {ac['desc']}" if ac.get("desc") else f"• *{ac['ref']}*"
        for ac in acs
    )
    return "\n".join(lines)


def _ac_list_selftest():
    acs = [
        {"ref": "AC-1", "desc": "User can log in with valid credentials"},
        {"ref": "AC-2", "desc": "Invalid password shows an error"},
    ]
    out = render_ac_list(acs)
    assert "AC-1" in out and "AC-2" in out, out
    assert "Acceptance Criteria (2)" in out, out
    assert "User can log in with valid credentials" in out, out
    # No testcase mapping — that stays in the Excel export only.
    assert "TC-" not in out, out
    # Guard against the to_slack AC-line-hijacking landmine (see docstring):
    # a real coverage-report rewrite would replace this text with a
    # "Tình trạng Test Case" table instead.
    assert "Tình trạng Test Case" not in out, out
    return out


if __name__ == "__main__":
    print(_selftest())
    print()
    print(_ac_list_selftest())
