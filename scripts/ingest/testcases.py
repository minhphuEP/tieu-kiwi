"""Ingest testcases from Excel (.xlsx) or CSV (.csv) into the graph.

Format (from the sample CDM test suite export):
  - One sheet = one test case, identified by a bracketed ID in the title,
    e.g. `[CDM_SPS_002] Verify Mark as preparing ...`
  - Row 1: column headers (Title, Priority, Pre-condition, Step_Description,
           Test_Data / DataCol_N, Step_ExpectedResult / Expected, ...)
  - Row 2: schema hints (REQUIRED* / optional) — SKIPPED.
  - Row 3+: data rows. Row 3 carries Title/Priority/Pre-condition + first step;
           subsequent rows are additional steps of the same test case.

This ingestor is column-order-tolerant: it matches headers case-insensitively
against a set of aliases. Extra columns are preserved verbatim into
props_json.raw_steps for auditability.

Usage:
    python scripts/ingest/testcases.py path/to/file.xlsx --project=CDM

Optional:
    --sprint=<ref>       auto-create Sprint node + `has` edge to each TestCase
    --sheet=<name>       ingest only one sheet
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

import argparse
import csv
import re
from pathlib import Path

import openpyxl
import psycopg

from tieukiwi import db


# Header name aliases (case-insensitive) → canonical field name.
HEADER_ALIASES = {
    "title":                "title",
    "test title":           "title",
    "priority":             "priority",
    "pre-condition":        "preconditions",
    "precondition":         "preconditions",
    "pre_condition":        "preconditions",
    "preconditions":        "preconditions",
    "step_description":     "step",
    "step description":     "step",
    "steps":                "step",
    "step":                 "step",
    "test_data":            "data",
    "test data":            "data",
    "data":                 "data",
    "step_expectedresult":  "expected",
    "step expected result": "expected",
    "expected":             "expected",
    "expected result":      "expected",
    "expected_result":      "expected",
    # Optional: comma/pipe-separated AC refs the TC covers, e.g.
    #   "AC-CDM-268-1, AC-CDM-268-2"
    # When present, we create AC -coveredBy-> TC edges after upserting.
    # Only put THIS value in row 3 (title row); subsequent step rows can be empty.
    "ac":       "ac_refs",
    "ac refs":  "ac_refs",
    "ac_refs":  "ac_refs",
    "acrefs":   "ac_refs",
    "acs":      "ac_refs",
    "coverage": "ac_refs",
    "covers":   "ac_refs",
    "covered ac": "ac_refs",
}


# Split "AC-1, AC-2 | AC-3" → ["AC-1", "AC-2", "AC-3"].
_AC_SPLIT_RE = re.compile(r"[,\|;]+")

def _parse_ac_refs(raw):
    if not raw or not isinstance(raw, str):
        return []
    parts = [p.strip() for p in _AC_SPLIT_RE.split(raw)]
    return [p for p in parts if p]

TEST_ID_RE = re.compile(r"\[([A-Za-z][A-Za-z0-9_\-]+)\]")


def _map_headers(header_row):
    """Return dict {canonical_field: column_index}."""
    mapping = {}
    for idx, h in enumerate(header_row):
        if not h:
            continue
        key = str(h).strip().lower()
        canonical = HEADER_ALIASES.get(key)
        if canonical and canonical not in mapping:
            mapping[canonical] = idx
    return mapping


def _extract_test_id(title):
    if not title:
        return None
    m = TEST_ID_RE.search(str(title))
    return m.group(1) if m else None


def _upsert_node(cur, type_, ref, project_id, props):
    """INSERT ... ON CONFLICT DO UPDATE. Preserves id across re-runs."""
    cur.execute(
        """
        INSERT INTO nodes (type, ref, project_id, props_json)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (project_id, ref) WHERE ref IS NOT NULL DO UPDATE
          SET props_json = EXCLUDED.props_json
        RETURNING id
        """,
        (type_, ref, project_id, psycopg.types.json.Json(props)),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    # Fallback: no unique index → SELECT existing
    cur.execute(
        "SELECT id FROM nodes WHERE type=%s AND ref=%s AND project_id=%s",
        (type_, ref, project_id),
    )
    return cur.fetchone()[0]


def _ensure_edge(cur, src_id, rel, dst_id):
    """Idempotent edge insert (no unique constraint on edges — use NOT EXISTS)."""
    cur.execute(
        """
        INSERT INTO edges (src_id, rel, dst_id)
        SELECT %s, %s, %s
        WHERE NOT EXISTS (
          SELECT 1 FROM edges WHERE src_id=%s AND rel=%s AND dst_id=%s
        )
        """,
        (src_id, rel, dst_id, src_id, rel, dst_id),
    )


def _sheets_from_csv(path):
    """Emulate the xlsx multi-sheet interface for a single CSV.

    A CSV holds ONE testcase's rows (no schema-hint row 2 assumed unless present).
    Yields a single (sheet_name, rows) pair.
    """
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            rows.append(tuple(cell if cell != "" else None for cell in row))
    return [(Path(path).stem, rows)]


def _sheets_from_xlsx(path, only_sheet=None):
    wb = openpyxl.load_workbook(path, data_only=True)
    for ws in wb.worksheets:
        if only_sheet and ws.title != only_sheet:
            continue
        yield ws.title, list(ws.iter_rows(values_only=True))


def parse_rows(rows):
    """Same logic as parse_sheet(), but takes a rows list instead of a worksheet."""
    if len(rows) < 3:
        return None
    header = rows[0]
    mapping = _map_headers(header)
    if "title" not in mapping or "step" not in mapping:
        return None
    # If row 2 is a schema hint (contains "REQUIRED" tokens), skip; else include.
    second = rows[1]
    if second and any(isinstance(c, str) and "REQUIRED" in c.upper() for c in second if c):
        data_rows = rows[2:]
    else:
        data_rows = rows[1:]

    first = data_rows[0]
    title = first[mapping["title"]] if mapping.get("title") is not None else None
    if not title:
        return None
    test_id = _extract_test_id(title)
    if not test_id:
        return None
    priority = first[mapping["priority"]] if mapping.get("priority") is not None else None
    preconditions = first[mapping["preconditions"]] if mapping.get("preconditions") is not None else None
    ac_refs_raw = first[mapping["ac_refs"]] if mapping.get("ac_refs") is not None else None

    step_col = mapping.get("step")
    exp_col = mapping.get("expected")
    steps = []
    raw_rows = []
    for r in data_rows:
        s = r[step_col] if step_col is not None and step_col < len(r) else None
        e = r[exp_col] if exp_col is not None and exp_col < len(r) else None
        if not s and not e:
            continue
        steps.append({"step": s, "expected": e})
        raw_rows.append({
            (header[i] if i < len(header) and header[i] else f"col{i}"): r[i]
            for i in range(len(r))
            if r[i] is not None
        })
    if not steps:
        return None
    steps_text = "\n".join(f"{i+1}. {s['step']}" for i, s in enumerate(steps) if s["step"])
    expected_text = "\n".join(f"{i+1}. {s['expected']}" for i, s in enumerate(steps) if s["expected"])
    return {
        "ref": test_id,
        "title": str(title).strip(),
        "priority": str(priority).strip() if priority else None,
        "preconditions": str(preconditions).strip() if preconditions else None,
        "steps": steps_text,
        "expected": expected_text,
        "raw_rows": raw_rows,
        "ac_refs": _parse_ac_refs(ac_refs_raw),
    }


def ingest(file_path, project_id, sprint_ref=None, only_sheet=None):
    ext = Path(file_path).suffix.lower()
    if ext == ".csv":
        sheets = _sheets_from_csv(file_path)
    elif ext == ".xlsx":
        sheets = _sheets_from_xlsx(file_path, only_sheet)
    else:
        raise SystemExit(f"Unsupported extension: {ext}. Use .xlsx or .csv")

    ingested = []

    with db.conn() as c:
        cur = c.cursor()

        sprint_id = None
        if sprint_ref:
            sprint_id = _upsert_node(cur, "Sprint", sprint_ref, project_id, {
                "_meta": {"extraction_source": "cli-arg", "source_file": str(file_path)},
            })

        for sheet_name, rows in sheets:
            tc = parse_rows(rows)
            if not tc:
                print(f"  [skip] {sheet_name!r} — no valid testcase")
                continue

            props = {
                "title": tc["title"],
                "type": "DataTable" if db.is_datatable_testcase(tc) else "Normal",
                "priority": tc["priority"],
                "preconditions": tc["preconditions"],
                "steps": tc["steps"],
                "expected": tc["expected"],
                "raw_rows": tc["raw_rows"],
                "source_sheet": sheet_name,
                # This ingest path is a SEED/FAKE fixture — used to populate
                # TC nodes so go_no_go / coverage_gap can be exercised end-to-
                # end without waiting for the real prod flow (Slack agent
                # gen_testcase + mark_reviewed) to produce them one by one.
                # Because the batch simulates "already-reviewed prod TCs",
                # stamp `review_status='qe_reviewed'` so strict-mode coverage
                # counts them as verified. Real prod TCs come in via
                # gen_testcase with review_status='draft' and only advance
                # via mark_reviewed.
                "review_status": "qe_reviewed",
                "_meta": {
                    "extraction_source": "excel-import" if ext == ".xlsx" else "csv-import",
                    "confidence": 1.0,
                    "source_file": str(file_path),
                },
            }
            if tc.get("ac_refs"):
                # Keep raw refs on the TC props so an auditor can see the mapping
                # source (row of the Excel) without walking edges.
                props["ac_refs"] = tc["ac_refs"]
            tc_id = _upsert_node(cur, "TestCase", tc["ref"], project_id, props)
            if sprint_id:
                _ensure_edge(cur, sprint_id, "has", tc_id)

            # Link this TC to every AC it covers (idempotent via _ensure_edge).
            # Missing ACs get logged as warnings but don't fail the ingest —
            # the AC might be extracted later from the BRD.
            covered = 0
            missing = []
            for ac_ref in (tc.get("ac_refs") or []):
                cur.execute(
                    "SELECT id FROM nodes WHERE type='AcceptanceCriterion' "
                    "AND ref=%s AND project_id=%s",
                    (ac_ref, project_id),
                )
                row = cur.fetchone()
                if row:
                    _ensure_edge(cur, row[0], "coveredBy", tc_id)
                    covered += 1
                else:
                    missing.append(ac_ref)

            ingested.append((tc["ref"], sheet_name, len(tc["raw_rows"])))
            cov_msg = f", covers {covered} AC" if covered else ""
            miss_msg = f" [missing AC: {missing}]" if missing else ""
            print(f"  [ok] {tc['ref']:14s} from {sheet_name!r} "
                  f"({len(tc['raw_rows'])} steps{cov_msg}){miss_msg}")

    print(f"\n[done] Ingested {len(ingested)} testcase(s) from {file_path}")
    return ingested


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("file", help="Path to testcase export (.xlsx | .csv)")
    ap.add_argument("--project", required=True, help="project_id (e.g. CDM)")
    ap.add_argument("--sprint", default=None, help="Sprint ref to attach these testcases to")
    ap.add_argument("--sheet", default=None, help="Only ingest this sheet")
    args = ap.parse_args()

    if not Path(args.file).exists():
        raise SystemExit(f"File not found: {args.file}")
    ingest(args.file, args.project, args.sprint, args.sheet)


if __name__ == "__main__":
    main()
