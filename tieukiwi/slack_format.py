"""Convert the agent's GitHub-flavored Markdown into Slack mrkdwn.

Slack mrkdwn is NOT GitHub Markdown: it has no tables, no "##" headings, and bold is
*single-star*, not **double-star**. This module rewrites an answer string so it renders
cleanly in Slack. In particular it:
  - never emits raw "| ... |" / "|---|" table rows,
  - renders 2-column key/value tables as "*Key:* value" (no "Field:"/"Value:" noise,
    and no broken "**Key*:*" markers even when the source cells are already bold),
  - renders acceptance-criteria coverage tables as one compact icon line per AC,
  - drops horizontal-rule "---" lines,
  - turns "## Heading" into a short bold line and **bold** into *bold*,
  - guarantees no leftover "**" anywhere in the output.

Entry point: markdown_to_mrkdwn(text) -> str
"""

import re

_BOLD_STARS = re.compile(r"\*\*(.+?)\*\*")
_BOLD_UNDERS = re.compile(r"__(.+?)__")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_SEPARATOR_CELL = re.compile(r":?-{1,}:?")
_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_AC_REF = re.compile(r"(?i)^ac[\s\-]?\d")
_MULTI_STAR = re.compile(r"\*{2,}")
_EMPTY_CELL = {"", "-", "—", "–", "n/a", "na", "none"}


def _norm(s):
    # Normalize inline emphasis to Slack mrkdwn: **x** / __x__ -> *x*.
    s = _BOLD_STARS.sub(r"*\1*", s)
    s = _BOLD_UNDERS.sub(r"*\1*", s)
    return s


def _plain(s):
    # Strip bold markers so a label can be re-wrapped once, cleanly (no double asterisks).
    s = _BOLD_STARS.sub(r"\1", s)
    s = _BOLD_UNDERS.sub(r"\1", s)
    return s.strip().strip("*").strip()


def _cells(line):
    # Split a markdown table row into stripped cell values (drop the outer pipes).
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator_cells(cells):
    # True for a markdown header/body separator row like |---|:--:|.
    nonempty = [c for c in cells if c != ""]
    return bool(nonempty) and all(_SEPARATOR_CELL.fullmatch(c) for c in nonempty)


def _status_icon(status):
    # Map a status string to a leading Slack icon. Negatives take precedence.
    if "✅" in status or ":white_check_mark:" in status:
        return ":white_check_mark:"
    if "❌" in status or ":x:" in status:
        return ":x:"
    s = status.strip().lower()
    if any(n in s for n in ("fail", "gap", "no ", "not ", "missing", "blocked", "no-go")):
        return ":x:"
    if any(p in s for p in ("pass", "done", "ok", "covered", "success", "go")):
        return ":white_check_mark:"
    return ":x:"


def _kv_lines(data_rows):
    # 2-column table -> "*Key:* value" lines (first column is the label).
    out = []
    for r in data_rows:
        key = _plain(r[0]) if len(r) > 0 else ""
        val = _norm(r[1]).strip() if len(r) > 1 else ""
        if key and val:
            out.append(f"*{key}:* {val}")
        elif key:
            out.append(f"*{key}:*")
        elif val:
            out.append(val)
    return out


def _coverage_table(header, data_rows):
    # Detect an acceptance-criteria coverage table and find its status column.
    # Returns (is_coverage, status_idx).
    status_idx = None
    if header:
        for k, h in enumerate(header):
            if "status" in h.lower():
                status_idx = k
                break
    if status_idx is None and data_rows:
        status_idx = len(data_rows[0]) - 1

    ac_header = bool(header) and bool(re.search(r"(?i)\b(ac|acceptance|criteri)", header[0] or ""))
    ac_cells = bool(data_rows) and sum(
        1 for r in data_rows if r and _AC_REF.match(_plain(r[0]))
    ) >= max(1, len(data_rows) // 2)

    return (status_idx is not None and (ac_header or ac_cells)), status_idx


def _coverage_lines(data_rows, status_idx):
    # One compact, scannable line per AC:  :icon: *AC-1* — TC-1 / TR-1 — *PASS*
    out = [f"*Test coverage ({len(data_rows)} acceptance criteria):*"]
    for r in data_rows:
        ac = _plain(r[0]) if r else ""
        status_raw = r[status_idx] if status_idx < len(r) else ""
        status = _plain(status_raw)
        detail_cells = [r[k] for k in range(1, len(r)) if k != status_idx]
        details = [_norm(c).strip() for c in detail_cells if c.strip().lower() not in _EMPTY_CELL]
        detail = " / ".join(d for d in details if d) if details else "no test case"
        out.append(f"{_status_icon(status_raw)} *{ac}* — {detail} — *{status or '—'}*")
    return out


def _generic_lines(header, data_rows):
    # Fallback for >2-col non-coverage tables: label cells with the header if present.
    out = []
    for r in data_rows:
        if header is not None:
            pairs = []
            for idx, val in enumerate(r):
                lbl = _plain(header[idx]) if idx < len(header) else ""
                v = _norm(val).strip()
                pairs.append(f"*{lbl}:* {v}" if lbl else v)
            line = "  •  ".join(p for p in pairs if p.strip())
        else:
            line = "  •  ".join(_norm(v).strip() for v in r if v.strip())
        if line:
            out.append(line)
    return out


def _format_table(run):
    # Turn a run of pipe-rows into Slack-friendly lines (no pipes, no --- rows).
    rows = [_cells(l) for l in run]
    sep_idx = next((k for k, c in enumerate(rows) if _is_separator_cells(c)), None)
    header = rows[sep_idx - 1] if (sep_idx is not None and sep_idx > 0) else None

    skip = set()
    if sep_idx is not None:
        skip.add(sep_idx)
        if header is not None:
            skip.add(sep_idx - 1)
    data_rows = [r for k, r in enumerate(rows) if k not in skip and not _is_separator_cells(r)]
    if not data_rows:
        return []

    ncols = max(len(r) for r in data_rows)
    if ncols <= 2:
        return _kv_lines(data_rows)

    is_coverage, status_idx = _coverage_table(header, data_rows)
    if is_coverage:
        return _coverage_lines(data_rows, status_idx)
    return _generic_lines(header, data_rows)


def _convert_inline(line):
    # **bold** / __bold__ -> *bold*, and "## Heading" -> *Heading*.
    line = _norm(line)
    m = _HEADING.match(line)
    if m:
        content = m.group(1).strip()
        line = f"*{content}*" if content else ""
    return line


def markdown_to_mrkdwn(text):
    if not text:
        return text
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # A run of consecutive pipe-containing lines is treated as a table block.
        if "|" in line and line.strip():
            run = []
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                run.append(lines[i])
                i += 1
            # Table lines are already normalized cell-by-cell — don't re-convert them.
            result.extend(_format_table(run))
            continue
        # Drop horizontal-rule lines ("---", "***", "___") — never print literal "---".
        if _HR.match(line):
            i += 1
            continue
        result.append(_convert_inline(line))
        i += 1

    out = "\n".join(result)
    # Backstop: guarantee no leftover run of "**" (unbalanced/edge cases) survives.
    return _MULTI_STAR.sub("*", out)


# --- tiny self-test (run: python -c "from tieukiwi import slack_format; print(slack_format._selftest())") ---

_SAMPLE = """## Jira: FRONT-3494

| **Field**    | **Value**                   |
|--------------|-----------------------------|
| **Key**      | FRONT-3494                  |
| **Summary**  | Optimize Memory Storefront  |
| **Type**     | Story                       |
| **Status**   | ✅ Done                      |
| **Priority** | High                        |
| **Assignee** | An Quoc Tran                |
| **Reporter** | Minh Phan                   |

---

### Test coverage

| AC       | Test Case | Test Run | Status       |
|----------|-----------|----------|--------------|
| **AC-1** | TC-1      | TR-1     | PASS         |
| AC-2     | —         | —        | coverage gap |
| AC-3     | TC-3      | TR-3     | FAIL         |

**Next actions:**
- Fix **AC-2**
"""


def _selftest():
    return markdown_to_mrkdwn(_SAMPLE)


if __name__ == "__main__":
    print(_selftest())
