# KB Update Guide — Tieu Kiwi (Tier 1 / Chroma)

> How to add or update knowledge base entries so the agent finds them for the
> right project and role. **You do not need to write any config** — metadata is
> inferred from where the file lives on disk.

## Concept

The KB (Chroma vector store) holds glossary, rules, templates, samples,
reference docs. The agent searches it during reviews.

Every `.md` / `.txt` / `.pdf` / `.docx` / `.doc` under `kb/` is auto-indexed
when you run `python scripts/seed/kb.py`. The **folder path** decides the metadata:

```
kb/
├── <PROJECT_ID>/                         scope=project, project_id=<PROJECT_ID>
│   ├── glossary.md                       doc_type=glossary  (from filename)
│   ├── <any-file>.md                     doc_type=reference (default)
│   ├── <ROLE>/                           role=<ROLE>
│   │   └── ...
│   ├── templates/*.md                    doc_type=template
│   └── samples/*.md                      doc_type=sample
│
└── _global/                              scope=global (used across all projects)
    ├── <any-file>.md
    └── <ROLE>/
        ├── templates/*.md
        └── samples/*.md
```

## Naming rules (STRICT)

- **`<PROJECT_ID>`** must match `nodes.project_id` you use in Postgres
  ingestion. If you run `scripts/ingest/requirements.py --project=CDM`, the RAG folder must
  be `kb/CDM/`. Case-sensitive.
- **`<ROLE>`** is one of `QE` / `PO` / `BO` / `DEV`. Uppercase only. `qe` will
  not be picked up as a role.
- **`_global`** has a leading underscore. Just `global` won't be detected.
- **Filenames**: kebab-case, no Vietnamese diacritics. Filename appears in
  logs / UI, so keep it descriptive.

## Common scenarios

### Scenario 1 — New KB for a new project

You have glossary + samples for `PROJ_XYZ`:

```bash
mkdir -p kb/PROJ_XYZ
cp glossary.md          kb/PROJ_XYZ/glossary.md
cp otp-samples.md       kb/PROJ_XYZ/samples/otp-samples.md   # → doc_type=sample
python scripts/seed/kb.py
```

Verify:
```bash
python -c "from tieukiwi.rag import search; \
  print(search('OTP', 3, project_id='PROJ_XYZ'))"
```

### Scenario 2 — Role-scoped KB for a project

Rules that only QE should see, scoped to `CDM`:

```bash
mkdir -p kb/CDM/QE
cp rules-testcase.md kb/CDM/QE/rules-testcase.md
python scripts/seed/kb.py
```

Agents running with `role="QE"` on that project retrieve it. Agents with
`role="PO"` filter it out.

### Scenario 3 — Company-wide (global) KB

Template shared across all projects:

```bash
# Global QE template
cp testcase-template.md kb/_global/QE/templates/testcase-template.md

# Global reference not tied to a role
cp coding-standards.md kb/_global/coding-standards.md

python scripts/seed/kb.py
```

Global docs are retrieved whenever `search()` is called with `include_global=True`.
The default `search_kb` tool sets this to `True`, so global docs surface
alongside project-scoped ones.

### Scenario 4 — Update an existing file

Just edit and re-seed. Chroma upserts by doc_id, so the vector is refreshed:

```bash
vim kb/CDM/glossary.md
python scripts/seed/kb.py
```

### Scenario 5 — Remove obsolete KB

Chroma does **not** notice deleted files. Use `--wipe`:

```bash
rm kb/CDM/deprecated.md
python scripts/seed/kb.py --wipe    # drop collection + re-index from current disk state
```

### Scenario 6 — Rename a project (CDM → CDM_TEAM)

Everything that references the project must move together (RAG + Postgres +
channel bindings):

```bash
# 1. RAG folder
mv kb/CDM kb/CDM_TEAM

# 2. Postgres nodes
docker exec -i tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi -c \
  "UPDATE nodes SET project_id='CDM_TEAM' WHERE project_id='CDM';"

# 3. Slack channel bindings
docker exec -i tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi -c \
  "UPDATE channel_project_map SET project_id='CDM_TEAM' WHERE project_id='CDM';"

# 4. Wipe + re-seed RAG (doc IDs change with the folder)
python scripts/seed/kb.py --wipe
```

## Verify checklist

After every seed:

```bash
# 1. Inspect seed output — is the metadata right?
python scripts/seed/kb.py 2>&1 | grep 'kb:'
# Expect lines like:
#   kb:CDM:glossary       scope=project project_id=CDM doc_type=glossary
#   kb:_global:QE:templates:testcase_template   scope=global role=QE doc_type=template

# 2. Filter search
python -c "
from tieukiwi.rag import search
print('CDM-only:',       search('OTP', 3, project_id='CDM'))
print('CDM + global:',   search('testcase', 3, project_id='CDM', include_global=True))
print('QE global:',      search('rule', 3, role='QE'))
"

# 3. Integration test (as the agent would call)
python -c "
from tieukiwi.tools import run_tool
print(run_tool('search_kb', {'query': 'OTP flow', 'k': 2},
               context={'project_id': 'CDM', 'role': 'QE'}))
"
```

## Cheat sheet

| I want | Put file at | Auto-tagged as |
|---|---|---|
| Glossary for project A | `kb/A/glossary.md` | scope=project, project_id=A, doc_type=glossary |
| QE review rules for project A | `kb/A/QE/rules.md` | + role=QE, doc_type=reference |
| Company-wide QE testcase template | `kb/_global/QE/templates/tc.md` | scope=global, role=QE, doc_type=template |
| Company-wide coding standard (any role) | `kb/_global/coding-standards.md` | scope=global, doc_type=reference |
| Sample data for project A | `kb/A/samples/example.md` | scope=project, project_id=A, doc_type=sample |
| Reference not tied to any project | `kb/_global/<file>.md` | scope=global, doc_type=reference |

## Supported file formats

Drop any of these into `kb/` — no conversion needed:

| Ext | Extractor | Notes |
|---|---|---|
| `.md` `.markdown` `.txt` | native | Lightest and best fidelity |
| `.pdf` | `pypdf` | Table extraction can miss cells; convert to `.md` for critical docs |
| `.docx` | `python-docx` | Preserves paragraphs + table cells |
| `.doc` | `textutil` | macOS only. On Linux/Windows, save as `.docx` first |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Search returns 0 results for `project_id=X` | Folder is `kb/x/` (wrong case) or `kb/X_TEAM/` (typo) | Rename folder → `python scripts/seed/kb.py --wipe` |
| Duplicate content in search | Same file in two locations | Delete one → `python scripts/seed/kb.py --wipe` |
| Deleted file still returned by agent | Chroma keeps orphan vectors | `python scripts/seed/kb.py --wipe` |
| Role folder didn't tag `role=` | Wrong case (`qe` not `QE`) | Fix case → re-seed |
| File not picked up | Extension not in `.md/.txt/.pdf/.docx/.doc` | Convert to a supported format |
| Metadata missing `project_id` | File in `kb/` root (not under a project folder) | Move into `kb/<PROJECT>/` |

## FAQ

**Q**: Same file needed for two projects — should I copy it into both folders?
**A**: No — put it in `kb/_global/` and let `include_global=True` fetch it.
Duplicating creates two vectors and clutters results.

**Q**: How big can a file be?
**A**: Chroma's default embedder (`all-MiniLM-L6-v2`) has a ~512 token context.
Long files (>2000 words) lose precision. For big BRDs, split by H2 headings
into multiple `.md` files before dropping into `kb/`.

**Q**: How often to re-seed?
**A**: Whenever you add/edit a file. It's idempotent, ~1s per file. No need to
automate for the hackathon.

**Q**: How do I know which docs the agent used to answer a question?
**A**: `search_kb` returns `(doc_id, text, metadata)` — the agent's response
should cite `doc_id`. Improve prompting if it doesn't.

**Q**: Can I filter by tags / labels beyond project + role + doc_type?
**A**: Not out of the box. Add fields to `metadata` in `scripts/seed/kb.py` (extend
`infer_kb_metadata`) and Chroma will filter on them. Ask storage-layer owner
if you need help.
