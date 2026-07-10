# Storage Guide — Tiểu Kiwi

> Cách lưu dữ liệu vào Tiểu Kiwi và commands để chạy. **Đọc file này trước** khi
> bắt đầu ingest bất cứ thứ gì. Chi tiết chuyên sâu ở:
> [`KB_GUIDE.md`](KB_GUIDE.md) (Chroma) · [`../data_ingestion/README.md`](../data_ingestion/README.md) (Postgres) · [`ontology.md`](ontology.md) · [`db_schema.md`](db_schema.md)

## 1. Kiến trúc 2 tầng — chọn đúng tầng để lưu

Tiểu Kiwi có **2 storage riêng biệt**, mỗi cái phục vụ 1 mục đích:

| Storage | Chứa gì | Query bằng | Populated bởi |
|---|---|---|---|
| **Postgres** (Tier 2 — Graph) | Project **artifacts**: Requirement, AC, TestCase, TestRun, Bug, Component, Feedback | SQL, graph traversal (structured) | `scripts/ingest/*.py` từ `data_ingestion/` |
| **Chroma** (Tier 1 — RAG) | Team **knowledge**: rules, glossary, templates, samples, lessons | Semantic similarity | `scripts/seed/kb.py` từ `kb/` + `skills/` |

**Quy tắc phân loại**:
- **Có ID/ref (VD `REQ-101`, `BUG-287`)?** → Postgres (artifact)
- **Có edge tới artifact khác (VD `AC coveredBy TestCase`)?** → Postgres
- **Là "kinh nghiệm / quy tắc / định nghĩa" chung?** → Chroma (knowledge)

Xem [`KB_GUIDE.md`](KB_GUIDE.md#concept) nếu vẫn confusing.

## 2. Decision tree: tôi có X, đặt ở đâu?

| Bạn có | Đặt ở đâu | Import bằng |
|---|---|---|
| Requirement mới (BRD từ Confluence) | `data_ingestion/requirements/<file>.md` | `scripts/ingest/requirements.py` |
| Testcase legacy bank (Excel/CSV) | `data_ingestion/testcases/<file>.xlsx` | `scripts/ingest/testcases.py` |
| Bug export (Jira JSON/Word) | `data_ingestion/bugs/<file>.json` | `scripts/ingest/bugs.py` (rồi `db.classify_bug()` phân loại) |
| Slack feedback (per artifact) | Postgres `nodes` type=Feedback + edge `about` | `db.add_node("Feedback", ...)` (Layer B) |
| Thread review state (bot memory) | Postgres `thread_state` | `memory.save_thread_state()` |
| Candidate rule chờ duyệt | Postgres `promotion_queue` | Direct SQL / Layer C tool |
| Lesson từ bug leaked (improvement) | Chroma `kb/<PROJ>/lessons/<bug>.md` | `scripts/seed/kb.py` |
| Rule QE (VD "testcase login phải cover 4 case") | `kb/_global/QE/rules-*.md` | `scripts/seed/kb.py` |
| Glossary dự án (VD định nghĩa OTP) | `kb/<PROJECT>/glossary.md` | `scripts/seed/kb.py` |
| Template (cách viết testcase chuẩn) | `kb/_global/QE/templates/*.md` | `scripts/seed/kb.py` |
| Sample testcase best-practice (few-shot) | `kb/<PROJECT>/samples/*.md` | `scripts/seed/kb.py` |
| Lesson từ bug (VD "phải test network delay") | `kb/<PROJECT>/lessons/<bug-ref>.md` | `scripts/seed/kb.py` |
| Coding standard chung không phân role | `kb/_global/<file>.md` | `scripts/seed/kb.py` |
| Testcase agent gen ra | (tự động vào Postgres) | Agent `gen_testcase` tool |
| Slack channel → project mapping | Postgres `channel_project_map` | `db.bind_channel()` (Python API) |

## 3. Prerequisites — chạy 1 lần

Trước khi ingest lần đầu, đảm bảo:

```bash
cd /path/to/tieu-kiwi

# 3.1 Docker + Postgres up
docker compose up -d

# 3.2 Apply schema + migrations (idempotent — chạy lại OK)
for f in db/schema.sql db/002_migration.sql db/003_migration.sql db/004_migration.sql; do
  docker exec -i tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi < "$f"
done

# 3.3 Python venv + deps
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3.4 Copy .env template + fill Anthropic key
cp .env.example .env
# Sửa .env: ANTHROPIC_API_KEY=sk-ant-xxx

# 3.5 Seed users (routing target — bắt buộc để `resolve_owner_slack` hoạt động)
python scripts/seed/users.py
```

## 4. Workflows theo tình huống

### 4.1 Có Requirement mới cho project CDM

```bash
# Bắt buộc — Postgres graph (để gen testcase, go_no_go work)
cp BRD-otp-fallback.docx data_ingestion/requirements/otp-fallback.docx
python scripts/ingest/requirements.py data_ingestion/requirements/otp-fallback.docx \
    --project=CDM \
    --sprint=SPR-26W8 \
    --us=CDM-999 \
    --us-title="OTP fallback v2"
```

**Kỳ vọng output**:
```
[req] REQ-OTP-FALLBACK — OTP fallback for login
  [ac] AC-1 — ...
  [ac] AC-2 — ...
  [impacts] COMP-auth-service
```

**Tùy chọn — cũng cho vào Chroma nếu muốn semantic search cross-requirement**:
```bash
cp data_ingestion/requirements/otp-fallback.docx kb/CDM/otp-fallback.docx
python scripts/seed/kb.py
```

**Verify**:
```bash
python -c "from tieukiwi import db; print(db.trace('REQ-OTP-FALLBACK', project_id='CDM'))"
```

### 4.2 Có bộ testcase legacy (Excel)

Chỉ Postgres — không cần cho vào Chroma:

```bash
cp CDM-testcases-2026Q2.xlsx data_ingestion/testcases/
python scripts/ingest/testcases.py \
    data_ingestion/testcases/CDM-testcases-2026Q2.xlsx \
    --project=CDM --sprint=SPR-26W8
```

**Optional** — curate 3-5 testcase "chuẩn nhất" làm few-shot cho agent gen:
```bash
# Copy nội dung best-practice sang .md dạng ngắn gọn
cat > kb/CDM/samples/tc-login-happy-path.md <<'EOF'
# Sample: Login happy path testcase

**Title**: [CDM_TC_LOGIN_001] Verify happy-path login

**Steps**:
1. Open /login
2. Enter valid credentials
3. Click Submit

**Expected**: Redirect to /dashboard, session cookie set.
EOF
python scripts/seed/kb.py
```

**Verify**:
```bash
python -c "from tieukiwi import db; \
  from tieukiwi.rag import search; \
  print('# Total TCs:', len([n for n in db.conn().__enter__().execute('SELECT ref FROM nodes WHERE type=\'TestCase\'').fetchall()])); \
  print('# Samples in KB:', search('login testcase', 2, project_id='CDM', doc_type='sample'))"
```

### 4.3 Có bug export từ Jira

Postgres bắt buộc, KB lesson tùy chọn (khuyến nghị nếu bug quan trọng):

```bash
# Postgres
cp CDM-287.doc data_ingestion/bugs/
python scripts/ingest/bugs.py data_ingestion/bugs/CDM-287.doc --project=CDM
```

**Nếu bug lộ ra gap về testcase → viết lesson** (5-10 dòng abstracted knowledge):
```bash
cat > kb/CDM/lessons/CDM-287.md <<'EOF'
# Lesson from CDM-287: OTP SMS delay

## What went wrong
Testcase login OTP không cover trường hợp network chậm >5s.
User thấy "OTP timeout" nhưng thực ra vẫn đang gửi.

## What to test next time
- Simulate network latency: delay 5s / 10s / 30s.
- Check timeout message phân biệt "timeout" vs "still sending".
- API contract: SMS gateway trả `pending` khác `failed`.

## Related
- Bug: CDM-287
- AC: AC-101-4 (OTP delivery within 30s)
EOF
python scripts/seed/kb.py
```

Lần sau khi gen testcase cho OTP feature, agent tự retrieve lesson này → cover regression.

### 4.4 Thêm rule QE mới (áp dụng mọi project)

Rule là **knowledge chung** — vào Chroma global:

```bash
mkdir -p kb/_global/QE
cat > kb/_global/QE/rules-testcase.md <<'EOF'
# QE Testcase Rules

## Login testcase coverage
Mọi testcase login phải cover 4 case:
1. Happy path
2. Wrong password
3. Wrong username
4. Lockout after 5 fails

## Priority mapping
- Blocker → Critical severity
- Business flow → High
- UX / edge → Medium/Low
EOF
python scripts/seed/kb.py
```

Áp dụng cross-project: agent QE ở channel nào (project nào) đều retrieve được (`include_global=True` mặc định).

### 4.5 Thêm glossary cho project

```bash
mkdir -p kb/CDM
cat > kb/CDM/glossary.md <<'EOF'
# CDM Glossary

## OTP
Mã 6 chữ số, hết hạn sau 5 phút. Gửi qua SMS (default) hoặc email.

## Sample flow
Reviewer → Add tracking → Ship → Receive confirmation.
EOF
python scripts/seed/kb.py
```

### 4.6 Testcase do agent gen (auto)

Agent gen qua tool `gen_testcase` (team A implement) — bạn không phải làm gì. Testcase sẽ:
- Auto tạo TestCase node trong Postgres với `_meta.review_status='draft'`
- Auto tạo edge `AC --coveredBy--> TestCase`

QE Lead review manual:
```bash
# Xem testcase drafts đang chờ review
docker exec tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi -c \
  "SELECT ref, props_json->'_meta'->>'review_status' AS status, props_json->>'title' \
   FROM nodes WHERE type='TestCase' AND project_id='CDM' \
     AND props_json->'_meta'->>'review_status'='draft';"

# Mark 1 testcase là verified
docker exec tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi -c \
  "UPDATE nodes SET props_json = jsonb_set(props_json, '{_meta,review_status}', '\"verified\"') \
   WHERE ref='TC-CDM-XXX';"
```

### 4.7 Bug classification — improvement loop

Sau khi ingest bug, gọi `db.classify_bug(bug_ref)` (hoặc agent tool `classify_bug`)
để phân loại bug và route vào đúng nhánh improvement:

```python
from tieukiwi import db
r = db.classify_bug("CDM-287", project_id="CDM")
# r['category'] = "caught_by_test" | "leaked_tc_missing" |
#                 "leaked_tc_not_run" | "leaked_tc_ran_missed" | "leaked_no_ac_link"
# r['improve']  = None | "gen_testcase" | "impact_analysis" | "execution_quality" | "manual_review"
```

**5 categories** (dựa trên **cấu trúc graph**, không phải heuristic):

| Category | Trigger | Improve pipeline | Curator action |
|---|---|---|---|
| `caught_by_test` | Bug có incoming `finds` từ TestRun | *(none — process worked)* | – |
| `leaked_tc_missing` | Bug `violates` AC, nhưng AC không có `coveredBy` | **`gen_testcase`** | Viết lesson vào `kb/<PROJ>/lessons/<bug>.md` để agent gen lần sau cover được |
| `leaked_tc_not_run` | AC có TC nhưng TC không có TestRun nào | **`impact_analysis`** | Đánh dấu TC/component là critical → future impact analysis prioritise |
| `leaked_tc_ran_missed` | AC có TC, TC có TestRun, mà bug vẫn lọt | **`execution_quality`** | Review TC assertions/steps — có thể quá yếu. Viết lesson tăng độ sâu test |
| `leaked_no_ac_link` | Bug không có `violates` edge nào | *(manual)* | Manual: link bug tới đúng AC trước, rồi classify lại |

**Workflow cho từng category** (curator viết lesson):

```bash
# Category: leaked_tc_missing — agent gen sót
cat > kb/CDM/lessons/CDM-287-gen-gap.md <<'EOF'
# Lesson: OTP flow needs to cover network delay

## Bug: CDM-287
Không add tracking number được khi variants=[] sau khi user chọn rồi clear.

## What was missing
Agent gen testcase cho "add tracking" chỉ cover happy path,
không cover edge case: user chọn variants rồi clear all → state ambiguous.

## What to test next time
- Sau khi clear all variants → verify form state reset đúng.
- Track "user-toggled" state riêng với "auto-cleared" state.

## Related
- AC: (link tới AC bị violate)
- Component: (link tới COMP-XXX)
EOF
python scripts/seed/kb.py
```

```bash
# Category: leaked_tc_not_run — impact analysis miss
cat > kb/CDM/lessons/CDM-999-impact-priority.md <<'EOF'
# Lesson: Cross-project OTP flow must always run in critical path

## Bug: CDM-999
Testcase TC-CDM-XXX exists nhưng chưa được execute trong sprint golive.

## What went wrong
Impact analysis không mark testcase này là critical dù nó cover
integration cross-project (auth ↔ notification-service).

## Rule for future
Bất kỳ testcase nào cover:
- Cross-project component
- Financial flow
- Auth flow
→ ALWAYS execute trong critical path, không skip vì time pressure.
EOF
python scripts/seed/kb.py
```

```bash
# Category: leaked_tc_ran_missed — testcase yếu
# Update testcase _meta để đánh dấu cần strengthen
docker exec tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi -c \
  "UPDATE nodes SET props_json = jsonb_set(
     props_json, '{_meta,quality_flag}', '\"weak_assertions\"'
   ) WHERE ref='TC-XXX';"
# Kèm lesson chung về assertion quality
cat > kb/_global/QE/lessons/assertion-depth.md <<'EOF'
# Lesson: Assertion depth in flows with side effects
...
EOF
python scripts/seed/kb.py
```

**Batch classify tất cả bug đã ingest**:

```bash
python <<'PY'
from tieukiwi import db
with db.conn() as c:
    rows = c.execute(
        "SELECT ref FROM nodes WHERE type='Bug' AND project_id='CDM'"
    ).fetchall()
for (ref,) in rows:
    r = db.classify_bug(ref, project_id='CDM')
    print(f"{ref:14s} → {r['category']:22s} improve={r['improve']}")
PY
```

Output cho biết mỗi bug nên đóng góp vào pipeline nào để improvement loop chạy có mục tiêu.

### 4.8 Slack feedback storage

Feedback từ Slack thread (user reply, curator decision, bot's hidden reasoning)
lưu ở **3 nơi khác nhau**, tùy nature và persistence:

| Nature | Đích | Bảng / Node | Đọc/ghi bằng |
|---|---|---|---|
| **Thread state** — tạm thời, per-review | Postgres | `thread_state (channel_id, thread_ts, state_json)` | `tieukiwi.memory.get_thread_state()` / `save_thread_state()` |
| **Feedback record** — cụ thể về 1 artifact, cần trace lâu dài | Postgres | `nodes` type=`Feedback` + edge `about` → target | `db.add_node("Feedback", ...)` + `db.add_edge(fb, "about", target)` |
| **Candidate rule** — feedback đủ mạnh để thành rule chung | Postgres | `promotion_queue` (candidate, source, status) | Direct SQL / promotion tool (Layer C, chưa build) |
| **Rule đã curator duyệt** | Chroma | `kb/<PROJ>/rules-*.md` hoặc `kb/_global/QE/rules-*.md` | `scripts/seed/kb.py` |

**Concrete flow — 1 feedback lifecycle**:

```
1. Slack thread: bot post review AC-101-3 → user reply "Nên nói 3 lần LIÊN TIẾP không phải tích luỹ"
      │
      ↓
2. Layer B (Slack handler):
   thread_state {"reviews": [...], "last_user_reply": "..."} → memory.save_thread_state(...)
      │
      ↓  (khi user_reply đủ có ý nghĩa)
3. Tạo Feedback node:
   fb = db.add_node("Feedback", ref="FB-<uuid>", props={
       "content": "AC-101-3 nên nói rõ 3 lần LIÊN TIẾP",
       "created_by": "U_slack_id",
       "channel_id": "C_xxx", "thread_ts": "17...",
   })
   db.add_edge(fb, "about", ac_101_3_id)
      │
      ↓  (curator thấy feedback có giá trị)
4. Push vào promotion_queue:
   INSERT INTO promotion_queue (candidate, source, status)
   VALUES ('AC phải phân biệt LIÊN TIẾP vs TÍCH LŨY khi mô tả điều kiện',
           '{"from_feedback": "FB-xxx", "from_thread": "C_xxx/17..."}'::jsonb,
           'pending');
      │
      ↓  (curator duyệt qua Slack button — Layer C, chưa build)
5. Ghi vào KB:
   cat > kb/_global/BA/rules-ac-writing.md <<EOF
   ## Rule: "Consecutive" vs "cumulative" in ACs
   Bất kỳ AC nào nói "sau N lần X" phải phân biệt rõ:
   - Consecutive (liên tiếp, reset khi có success ở giữa)
   - Cumulative (tích luỹ, không reset)
   Nguồn: FB-xxx (từ CDM channel review 2026-07-03).
   EOF
   python scripts/seed/kb.py
```

**Khi nào lưu vào đâu**:

- **Ephemeral (thread_state)**: state review của bot, ai đã accept/reject, timestamp
  — dùng để bot resume conversation. TTL: có thể xoá sau 30-90 ngày.
- **Feedback node**: 1 nhận xét cụ thể → cần trace lâu dài (VD "AC-101-3 unclear per user X on 2026-07-03"). Giữ vĩnh viễn (audit).
- **promotion_queue**: candidate rule chờ curator. Xoá khi approve (đã promote vào KB) hoặc reject.
- **KB rule (Chroma)**: rule đã active, applies to future artifacts. Giữ vĩnh viễn cho tới khi bị demote.

**Manual write khi thấy feedback quan trọng** (Slack bot chưa build):

```python
from tieukiwi import db

# Ghi 1 Feedback node
fb_id = db.add_node("Feedback",
    ref="FB-2026-07-03-001",
    props={
        "content": "AC-101-3 unclear about consecutive vs cumulative fails",
        "created_by": "U03_QE_CUONG",
        "channel_id": "C_CDM_REVIEW",
        "thread_ts": "1720000000.123456",
        "status": "pending",
    })

# Link về artifact bị feedback
with db.conn() as c:
    ac_id = c.execute("SELECT id FROM nodes WHERE ref='AC-101-3'").fetchone()[0]
db.add_edge(fb_id, "about", ac_id)

# Nếu feedback đáng promote → push vào queue
with db.conn() as c:
    c.execute(
        """INSERT INTO promotion_queue (candidate, source)
           VALUES (%s, %s::jsonb)""",
        ("AC phải phân biệt consecutive vs cumulative",
         '{"from_feedback": "FB-2026-07-03-001"}'),
    )
```

Xem `routing.resolve_owner_slack()` để biết agent sẽ ping ai khi có gap ở artifact
này (Feedback node hop qua `about` edge để lookup owner của entity đích).

## 5. Verify sau khi ingest

Câu SQL / Python cheatsheet:

```bash
# Đếm node theo type
docker exec tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi -c \
  "SELECT type, COUNT(*) FROM nodes WHERE project_id='CDM' GROUP BY type ORDER BY 1;"

# Đếm edges theo relation
docker exec tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi -c \
  "SELECT rel, COUNT(*) FROM edges GROUP BY rel ORDER BY 1;"

# Coverage gap của 1 requirement
python -c "from tieukiwi import db; print(db.coverage_gap(project_id='CDM'))"

# Trace 1 requirement (Req → AC → TC → Run → Bug)
python -c "from tieukiwi import db; print(db.trace('REQ-XXX', project_id='CDM'))"

# Go/No-Go decision
python -c "from tieukiwi import db; print(db.go_no_go('REQ-XXX', project_id='CDM'))"

# Bug blast radius
python -c "from tieukiwi import db; print(db.bug_blast_radius('BUG-XXX', project_id='CDM'))"

# Classify bug — improvement loop (caught_by_test | leaked_tc_missing | leaked_tc_not_run | leaked_tc_ran_missed)
python -c "from tieukiwi import db; import json; print(json.dumps(db.classify_bug('BUG-XXX', project_id='CDM'), indent=2))"

# KB semantic search
python -c "from tieukiwi.rag import search; \
  [print(d[0], d[2]) for d in search('testcase OTP', 3, project_id='CDM', include_global=True)]"

# Routing: node → owner
python -c "from tieukiwi import db, routing; \
  n = db.conn().__enter__().execute(\"SELECT id FROM nodes WHERE ref='BUG-XXX'\").fetchone()[0]; \
  print(routing.resolve_owner_slack(n))"
```

## 6. Reset / rebuild — dev only

Khi cần clean state (VD demo, test lại):

```bash
# Wipe graph (nodes/edges/users)
python scripts/seed/reset.py --yes
python scripts/seed/users.py    # seed users lại

# Wipe Chroma (KB)
python scripts/seed/kb.py --wipe

# Re-ingest tất cả
python scripts/ingest/requirements.py data_ingestion/requirements/<file> --project=CDM ...
python scripts/ingest/testcases.py data_ingestion/testcases/<file> --project=CDM
python scripts/ingest/bugs.py data_ingestion/bugs/<file> --project=CDM
```

## 7. Wire Slack channel với project

Layer B (Slack bot) cần biết channel → project. Bind 1 lần cho mỗi channel:

```python
from tieukiwi import db
db.bind_channel("C0123XYZ", "CDM", team_id="T01", note="wired 2026-07-03 by <bạn>")
```

Sau đó Slack handler tự resolve:
```python
proj = db.project_for_channel(event["channel"]) or DEFAULT_PROJECT
answer = agent.ask(text, project_id=proj, role="QE")
```

## 8. Idempotency & re-run

Tất cả script an toàn khi chạy lại:

| Script | Behavior khi re-run |
|---|---|
| `scripts/ingest/*.py` | Upsert theo `(project_id, ref)` — không tạo duplicate |
| `scripts/seed/kb.py` | Upsert theo doc_id — file bị xoá thì cần `--wipe` |
| `scripts/seed/users.py` | `ON CONFLICT (slack_id) DO NOTHING` |
| `scripts/seed/reset.py` | Xoá tất cả nodes/edges/users (destructive) |

## 9. Troubleshooting

| Triệu chứng | Fix |
|---|---|
| `search_kb` return 0 dù có file | Chưa chạy `scripts/seed/kb.py` sau khi thêm file |
| `go_no_go` return `NOT_FOUND` | `project_id` không khớp với node's project_id (case-sensitive) |
| KB search trả file dự án khác | `project_id` sai hoặc quên set `include_global` |
| `psycopg.errors.UniqueViolation` khi ingest | Đã có node với `(project_id, ref)` cũ — script sẽ upsert, không crash |
| `Cannot read <file>: textutil not found` | `.doc` chỉ chạy trên macOS. Convert sang `.docx` |
| Chroma download `all-MiniLM-L6-v2` chậm | Lần đầu ~80MB, sau đó cache |
| `resolve_owner_slack` return `None` | User table thiếu role tương ứng — chạy `scripts/seed/users.py` |

## 10. Cross-reference

- **Chi tiết KB** (folder convention, metadata inference): [`KB_GUIDE.md`](KB_GUIDE.md)
- **Chi tiết Postgres ingest** (column mapping, format specs): [`../data_ingestion/README.md`](../data_ingestion/README.md)
- **Ontology + relations**: [`ontology.md`](ontology.md)
- **ERD Postgres**: [`db_schema.md`](db_schema.md)
- **Team announcement (2026-07-03)**: [`CHANGELOG.md`](CHANGELOG.md)
