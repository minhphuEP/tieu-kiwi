"""Write TestCase drafts to an Excel workbook following the Testomat.io import
format documented in kb/_global/QE/templates/testcase_template.md — the write
side inverse of scripts/ingest/testcases.py.

Interface:
  export_excel(testcases) -> bytes   # .xlsx workbook, ready to upload

Draft schema per testcase (see tieukiwi/testcase_gen.py):
  {"ref", "ac_refs", "title", "priority", "precondition", "steps": [...],
   "data_variants": [...]}
Empty `data_variants` -> row appended to the shared Normal_TestCases sheet.
Non-empty `data_variants` -> its own sheet named after `ref` (data-driven),
where each variant's `values` dict may include an "Expected" key for the
per-row expected result column (kept last).
"""
import io

import openpyxl

_NORMAL_SHEET = "Normal_TestCases"
_NORMAL_HEADERS = ["Title", "Priority", "Pre-condition", "Step_Description",
                   "Test_Data", "Step_ExpectedResult"]
_DATA_SEP_TEXT = "DATA TABLE  ▼  one row = one set of test data"


def export_excel(testcases):
    """testcases: list of draft-schema dicts. Returns the .xlsx workbook as bytes."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # drop the default blank sheet

    normal_tcs = [tc for tc in testcases if not tc.get("data_variants")]
    data_driven_tcs = [tc for tc in testcases if tc.get("data_variants")]

    if normal_tcs:
        _write_normal_sheet(wb, normal_tcs)
    for tc in data_driven_tcs:
        _write_data_driven_sheet(wb, tc)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write_normal_sheet(wb, testcases):
    ws = wb.create_sheet(_NORMAL_SHEET)
    ws.append(_NORMAL_HEADERS)
    for tc in testcases:
        steps = tc["steps"] or [{"description": "", "expected": ""}]
        first = steps[0]
        ws.append([tc["title"], tc["priority"], tc.get("precondition", ""),
                   first["description"], "(single-action TC — no DT)", first["expected"]])
        for step in steps[1:]:
            ws.append(["", "", "", step["description"], "", step["expected"]])


def _write_data_driven_sheet(wb, tc):
    ws = wb.create_sheet(tc["ref"])
    steps = tc["steps"] or [{"description": "", "expected": ""}]
    first = steps[0]
    ws.append([tc["title"], tc["priority"], tc.get("precondition", ""),
               first["description"], "Description", first["expected"]])
    for step in steps[1:]:
        ws.append(["", "", "", step["description"], "", step["expected"]])
    ws.append([_DATA_SEP_TEXT])
    all_keys = {k for v in tc["data_variants"] for k in v["values"]}
    variant_cols = sorted(all_keys - {"Expected"})
    ws.append(["", "", "", "", "Description"] + variant_cols + ["Expected"])
    for variant in tc["data_variants"]:
        row = ["", "", "", "", variant.get("label", "")]
        row += [variant["values"].get(col, "") for col in variant_cols]
        row.append(variant["values"].get("Expected", ""))
        ws.append(row)


def _selftest():
    testcases = [
        {"ref": "TC-1", "ac_refs": ["AC-1"], "title": "[TC-1] Happy path", "priority": "High",
         "precondition": "1. Logged in.",
         "steps": [{"description": "Click submit", "expected": "See success"}],
         "data_variants": []},
        {"ref": "TC-2", "ac_refs": ["AC-2"], "title": "[TC-2] Field variants", "priority": "Medium",
         "precondition": "",
         "steps": [{"description": "Enter value", "expected": "Validated"}],
         "data_variants": [
             {"label": "empty", "values": {"Field": "", "Expected": "Error shown"}},
             {"label": "valid", "values": {"Field": "abc", "Expected": "Accepted"}},
         ]},
    ]
    data = export_excel(testcases)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert set(wb.sheetnames) == {"Normal_TestCases", "TC-2"}, wb.sheetnames
    normal = wb["Normal_TestCases"]
    assert [c.value for c in normal[1]] == _NORMAL_HEADERS
    assert normal["A2"].value == "[TC-1] Happy path"
    dd = wb["TC-2"]
    assert dd["A1"].value == "[TC-2] Field variants"
    header_row = [c.value for c in dd[3]]
    assert header_row[4] == "Description" and header_row[-1] == "Expected", header_row
    assert dd[4][4].value == "empty" and (dd[4][5].value or "") == "" and dd[4][-1].value == "Error shown"
    return "ok"


if __name__ == "__main__":
    print(_selftest())
