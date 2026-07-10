# Graph Insert Guide — dành cho đội tool

> Tài liệu này là **hợp đồng** khi bất kỳ pipeline/tool nào (`gen_testcase`,
> `fetch_confluence`, TMS importer, bug ingest…) insert node/edge vào Postgres
> graph của Tiểu Kiwi. Tuân thủ để `go_no_go`, `get_requirement`, `trace` không
> bị lệch dữ liệu — và để agent LLM không hallucinate.

Bổ sung cho:
- [`docs/db_schema.md`](db_schema.md) — schema chi tiết (columns, indexes).
- [`docs/STORAGE_GUIDE.md`](STORAGE_GUIDE.md) — quyết định Postgres vs Chroma.
- [`CLAUDE.md`](../CLAUDE.md) — ontology + convention gốc.

---

## 1. Convention `ref` cho từng loại node

| Type | Format ref | Ví dụ | Nguồn |
|---|---|---|---|
| `Requirement` | `<PROJECT>-<num>` (Jira issue key) | `CDM-199` | Jira REST |
| `UserStory` | `<PROJECT>-<num>` (Jira key) | `CDM-275` | Jira REST |
| `AcceptanceCriterion` | `AC-<REQ_KEY>-<idx>` | `AC-CDM-268-1` | LLM extract từ PRD hoặc human seed |
| `BRD` | `CFL-<confluence_page_id>` | `CFL-2541551769` | Confluence REST |
| `TestCase` | `TC-<REQ_KEY>-<slug>` **hoặc** `<PROJECT>_<Feature>_<num>` | `TC-CDM-268-A`, `CDM_DupScript_001` | Excel export TMS **hoặc** `gen_testcase` |
| `TestRun` | **BẮT BUỘC prefix `TR-`**, lý tưởng `TR-<tms_run_uuid>` | `TR-4c27ee5e-a1a0-475f-9196-f8217c5b7be6` | TMS Selless |
| `Bug` | `<REQ_KEY>-<seq>` **hoặc** Jira bug key | `CDM-302-1`, `CDM-311` | Jira / ingest bugs |
| `Component` | `COMP-<slug>` | `COMP-auth-service` | LLM extract từ PRD |
| `Sprint` | `SPR-<yy>W<ww>` | `SPR-26W7` | Manual / Jira sprint API |

**Rule cứng:**
- `TestRun.ref` **không được** trùng format Jira issue key (`^[A-Z]+-\d+$`) — sẽ collision với `Requirement`/`Bug` under unique index `(project_id, ref)`. Luôn có prefix `TR-`.
- Với `Bug`, không dùng lại chính Jira key của `Requirement` — dùng format `<REQ>-<seq>` (VD `CDM-302-1`) để bug đầu tiên tìm thấy khi test CDM-302.
- Không thay đổi `ref` sau khi insert. Ref là identity ổn định.

---

## 2. `project_id` — luôn derive từ ref

**Rule**: `project_id = ref` cắt trước dấu `-` đầu tiên (Jira project key).

```python
from tieukiwi import db
db.project_from_ref("CDM-199")     # → "CDM"
db.project_from_ref("CDM-302-1")   # → "CDM"  (bug)
db.project_from_ref("TR-CDM-266")  # → None   (không phải Jira key; caller phải strip TR-)
```

- Với `TestRun` ref = `TR-<tms_uuid>`: phải nhận `project_id` từ Requirement/TestCase parent, KHÔNG derive từ UUID.
- `Component`, `Sprint`, `BRD`: `project_id` phải được set thủ công vì ref không carry Jira prefix.
- `null project_id` chỉ chấp nhận cho **shared node** (Component dùng chung nhiều project). Mặc định luôn set.

---

## 3. `props_json._meta` — provenance bắt buộc

Mọi node insert bởi pipeline (không phải human) **PHẢI** có `_meta`:

```json
{
  "title": "...",           // hoặc "detail", "desc", tuỳ node
  "_meta": {
    "extraction_source": "llm:claude-sonnet-4-6" | "jira-rest" | "confluence-rest" | "excel-import" | "tms-selless" | "human",
    "confidence": 0.87,     // 0..1; 1.0 cho human/excel
    "source_file": "https://... | path/to/file.xlsx",
    "review_status": "draft" | "verified" | "rejected",
    "created_at": "2026-07-07T…"    // optional; DB có created_at riêng
  }
}
```

- `review_status`:
  - `draft` — LLM-extracted, chưa human duyệt.
  - `verified` — human OK.
  - `rejected` — human loại bỏ; downstream nên skip.
- Node bị thiếu `_meta` = human/legacy (default trust).

---

## 4. Insert node — luôn dùng `upsert_node_by_ref`

**KHÔNG** viết `INSERT` trực tiếp trong pipeline mới. Dùng helper:

```python
from tieukiwi import db

node_id = db.upsert_node_by_ref(
    "TestCase",
    "TC-CDM-268-A",
    props={
        "title": "Duplicate script — happy path",
        "steps": ["Click Duplicate", "Chọn product", "Save"],
        "expected": "Toast success + navigate to Draft Detail",
        "_meta": {
            "extraction_source": "excel-import",
            "confidence": 1.0,
            "source_file": "kb/CDM/QE/TC-2026-06-30.xlsx",
            "review_status": "verified",
        },
    },
    project_id=db.project_from_ref("TC-CDM-268-A") or "CDM",   # None → set thủ công
)
```

Đặc điểm:
- Idempotent. Chạy lại cùng `(type, ref)` → UPDATE `props_json` chứ không tạo trùng.
- `project_id` chỉ được set khi INSERT — UPDATE không di chuyển node giữa tenant (safety).

---

## 5. Insert edge — luôn dùng `ensure_edge`

```python
from tieukiwi import db

tc_id = db.node_id_for("TC-CDM-268-A", type_="TestCase")
ac_id = db.node_id_for("AC-CDM-268-1", type_="AcceptanceCriterion")

db.ensure_edge(ac_id, "coveredBy", tc_id)   # AC → coveredBy → TestCase
```

Idempotent — chạy lại không tạo edge duplicate. Edges lưu theo `id` (không phải `ref`), nên đổi tên `ref` node không phá edges.

**Đúng chiều edge (theo ontology CLAUDE.md):**
- `Requirement --has--> AC`
- `AC --coveredBy--> TestCase`
- `TestCase --executedBy--> TestRun`
- `TestRun --finds--> Bug`
- `Bug --affects--> Component`
- `Bug --violates--> AC`
- `Requirement --derivedFrom--> BRD`
- `Requirement --impacts--> Component`

Sai chiều → `trace()` / `coverage_gap()` không thấy → agent trả lời sai.

---

## 6. Postgres vs Chroma — data nào ở đâu

| Data | Postgres (graph) | Chroma (RAG) |
|---|---|---|
| Requirement metadata (title, status, assignee) | ✅ `props_json` | ❌ |
| BRD/PRD full body | ❌ | ✅ `kb/<PROJECT>/BRD/<page_id>.md` |
| BRD metadata (page_id, version, `content_hash`, `section_anchor`, `content_preview`) | ✅ | ❌ |
| AC detail (nếu dài, > 200 chars) | ✅ `props_json.detail` | ✅ optional index |
| TestCase steps | ✅ `props_json.steps` | ❌ |
| TestRun status/environment | ✅ | ❌ |
| Bug title/severity/steps | ✅ | ❌ |
| KB rules (curator promoted) | ✅ `kb_rules` | ✅ auto-indexed |
| Team convention docs, glossary, templates | ❌ | ✅ `kb/**` |

**Quy tắc:**
- **Metadata + relationships** → Postgres.
- **Full text để RAG search** → Chroma. Chroma id ổn định: `kb:<PROJECT>:<doc_type>:<page_id>`.
- **Không duplicate full body vào Postgres** — dùng `content_preview` (~500 char) + `content_hash`.

Khi update BRD từ Confluence: dùng `content_hash` làm short-circuit. Hash không đổi → skip cả 3 lớp (Postgres update, file write, Chroma reindex).

---

## 7. Worked example — pipeline TestCase (đội tool tham khảo)

Kịch bản: `gen_testcase(requirement_ref="CDM-199")` gen ra 3 TC từ AC linked.

```python
from tieukiwi import db

REQ_REF = "CDM-199"
PROJECT = db.project_from_ref(REQ_REF)   # → "CDM"

# 1) Load context — get_requirement đã trả sẵn AC list + coverage flag
ctx = db.get_requirement(REQ_REF, project_id=PROJECT)
if not ctx["found"]:
    raise SystemExit(f"Requirement {REQ_REF} chưa có trong graph. Chạy fetch_jira trước.")

uncovered_acs = [ac for ac in ctx["acceptance_criteria"] if not ac["coverage"]["has_testcase"]]
if not uncovered_acs:
    print("Không có AC nào cần gen TC. Đầy đủ coverage.")
    return

# 2) Với mỗi AC, gen TC + tạo node + edge
for ac in uncovered_acs:
    generated = call_llm_to_gen_tc(ac["detail"])   # trả list of {title, steps, expected}
    for idx, tc in enumerate(generated, start=1):
        tc_ref = f"TC-{REQ_REF}-{ac['ref'].split('-')[-1]}-{idx:02d}"   # TC-CDM-199-1-01
        tc_id = db.upsert_node_by_ref(
            "TestCase",
            tc_ref,
            props={
                "title":    tc["title"],
                "steps":    tc["steps"],
                "expected": tc["expected"],
                "_meta": {
                    "extraction_source": "llm:claude-sonnet-4-6",
                    "confidence": 0.8,
                    "source_file": f"tool:gen_testcase({REQ_REF})",
                    "review_status": "draft",
                },
            },
            project_id=PROJECT,
        )
        ac_id = db.node_id_for(ac["ref"], type_="AcceptanceCriterion")
        db.ensure_edge(ac_id, "coveredBy", tc_id)

# 3) Verify: chạy lại get_requirement, warnings phải giảm
after = db.get_requirement(REQ_REF, project_id=PROJECT)
print(f"AC coverage: {sum(1 for a in after['acceptance_criteria'] if a['coverage']['has_testcase'])}/{len(after['acceptance_criteria'])}")
```

Quan sát:
- `_meta.review_status = "draft"` — TC mới gen phải chờ human confirm rồi mới lên `verified`.
- `go_no_go` (strict mode tương lai) sẽ ignore TC status=draft.
- Không tạo TestRun ở đây — TestRun chỉ sinh ra khi thực sự chạy test trên TMS.

---

## 8. Anti-patterns — đã từng gặp, KHÔNG lặp lại

| Sai | Đúng | Vì sao |
|---|---|---|
| `TestRun.ref = "CDM-266"` | `TestRun.ref = "TR-CDM-266"` hoặc `TR-<tms_uuid>` | Trùng với Requirement CDM-266 → violate unique index |
| Insert TestRun cho mỗi Jira issue có issuetype="Test" | Chỉ insert TestRun khi TMS Selless report có run thật | 1 Jira ticket ≠ 1 TestRun. TestRun = 1 lần chạy thực tế |
| `db.upsert_node_by_ref(...)` không set `project_id` | Truyền `project_id=db.project_from_ref(ref)` | Node NULL project_id → invisible với multi-tenant queries |
| Bịa AC1/AC2 khi graph chưa có AC | Gọi `get_requirement`, echo `warnings` cho user | LLM hallucinate = báo cáo sai coverage |
| Lưu full PRD body vào `props_json` của BRD | Chỉ lưu `content_preview` + `content_hash`, full body → file `kb/CDM/BRD/<page_id>.md` | Postgres không tối ưu semantic search, blow up row size |
| Tạo edge bằng `db.add_edge` (không check duplicate) | Dùng `db.ensure_edge` | Chạy lại pipeline → edge duplicate → trace() ra output nhân đôi |

---

## 9. Checklist "graph đã sẵn sàng cho `go_no_go`"

Với 1 Requirement `REQ_REF`, `go_no_go` chỉ trả GO nếu graph có đủ:

- [ ] Requirement node exists (từ `fetch_jira`)
- [ ] ≥1 AC linked via `has` (từ `fetch_confluence` hoặc human)
- [ ] Mỗi AC có ≥1 TestCase linked via `coveredBy` (từ Excel import / `gen_testcase`)
- [ ] Mỗi TestCase có ≥1 TestRun via `executedBy` với `status="pass"` (từ TMS)
- [ ] Không có Bug open với severity ∈ `("critical", "high")` linked vào Requirement/AC

Chạy `get_requirement(REQ_REF)` — nếu `warnings` rỗng và mọi AC có `coverage.has_testcase=True` thì bước 3 xong; còn 4/5 do `go_no_go` tự check.

---

## 10. Debug commands

```bash
# Xem 1 Requirement full context
docker exec -i tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi -c \
  "SELECT jsonb_pretty(props_json) FROM nodes WHERE ref='CDM-199';"

# Đếm nodes theo project + type
docker exec -i tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi -c \
  "SELECT project_id, type, COUNT(*) FROM nodes GROUP BY 1,2 ORDER BY 1,2;"

# Node lẻ (không edge nào)
docker exec -i tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi -c \
  "SELECT id, type, ref FROM nodes n WHERE NOT EXISTS
    (SELECT 1 FROM edges e WHERE e.src_id=n.id OR e.dst_id=n.id);"

# Detect ref format vi phạm (TestRun không có prefix TR-)
docker exec -i tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi -c \
  "SELECT id, ref FROM nodes WHERE type='TestRun' AND ref !~ '^TR-';"
```

---

*Cập nhật lần cuối: 2026-07-10 (post-merge phuong_qe ↔ master). Migrations hiện tại: `002` → `006` (migration `007` đã bị bỏ, xem STORAGE_GUIDE §6.2.6). AC node giờ có `section_anchor` + `section_title` props sau khi extract — xem `jira_ingest._diff_and_upsert_acs`. Khi thêm loại node mới hoặc đổi convention, update file này TRƯỚC khi merge pipeline mới.*
