# Hash-gate — ingest freshness

`ingest_jira_ticket` short-circuits when nothing decision-relevant has changed
on a ticket, so a Slack `@tieu_kiwi CDM-321 sắp go-live chưa?` chat costs
~500 ms instead of 4–15 s when the ticket is cached.

The gate is **content-based**, not time-based. No TTLs to tune, no arbitrary
"stale after 15 min" thresholds. A cache hit means "we have proof nothing
material changed"; a cache miss means "we have proof something did."

**Source of truth**: `tieukiwi/jira_ingest.py` — `_story_hash`,
`_bug_table_hash`, `_check_brd_freshness`, and the check block at the top of
`ingest_jira_ticket`.

---

## 3 tiers, independent

| Tier | What it hashes | Where stored | Cost when it runs |
|---|---|---|---|
| 1 — **Story** | Canonical fields of the parent Jira issue | `Requirement.props.story_hash` | 0 REST calls beyond the one we always make |
| 2 — **Bug-table** | Raw table cells of each `[Bug]` subtask description | `Requirement.props.bug_container_hashes[subtask_key]` | +1 REST call per `[Bug]` subtask |
| 3 — **BRD-drift** | Confluence `version.number` of each linked BRD | Compared against stored `BRD.props.version` | +1 cheap metadata REST call per linked BRD |

Only tier 1 can short-circuit the whole pipeline. Tiers 2 and 3 are
*inner-loop optimisers*: they run only when tier 1 is dirty (or force=True),
and skip work per-item within the pipeline.

---

## Tier 1 — story hash

### Fields IN

Extracted from the Jira `fields.*` response in `_story_hash(fields)`:

```
summary                   fields.summary
status.name               fields.status.name              e.g. "Beta Ready"
assignee.displayName      fields.assignee.displayName
priority.name             fields.priority.name
description_text          fields.description (ADF) → adf.to_pretty_text
confluence_urls           extracted from description ADF, sorted
subtasks[]                for each stub: {key, summary, status.name}, sorted by key
```

### Fields OUT (excluded on purpose)

- `fields.updated` — Jira touches this on view / minor edits, too noisy
- Custom fields — schema varies per instance
- Comment count, watchers, votes — not decision-relevant
- Reporter — rare change, decision-irrelevant

### Effect

`sha256(json.dumps(canonical, sort_keys=True))[:16]` stored on the
Requirement. On the next call:

```
new_hash == existing_hash → return status='cached_fresh'  (~500 ms total)
new_hash != existing_hash → run the full pipeline
```

---

## Tier 2 — bug-container hash

Story hash includes subtask **stubs** (key / summary / status) but not their
**descriptions**. A `[Bug]` subtask's description carries the actual bug
table — that content isn't visible at tier 1.

`_bug_table_hash(description_adf)` hashes the raw table cells (via
`adf.extract_tables`). Stored per-subtask in
`Requirement.props.bug_container_hashes = {"CDM-286": "abc123", ...}`.

**When it runs**: after tier 1 misses. In the bug-container loop, we fetch
the full subtask JSON, hash the table, and compare. Same hash → skip
re-parse. Different hash → re-parse + `_upsert_bugs_from_table`.

**Trade-off**: tier 2 does NOT run when tier 1 is fresh. If a QE adds a bug
row without changing the subtask's summary or status, tier 1 still says
"cached_fresh" and the new row is missed until the next story-level change
(or the user says "cập nhật CDM-268" to force a refresh).

Making tier 2 top-level would cost N extra REST calls per Slack message
(one per `[Bug]` subtask), so the cheap-check-first design wins in practice.

---

## Tier 3 — BRD (Confluence) drift

Even when Jira is unchanged, PO may have edited the Confluence PRD. The BRD
node in Postgres stores the `version.number` at ingest time.

`_check_brd_freshness(req_node_id)` walks `Requirement -derivedFrom-> BRD`
edges, calls `confluence.get_page_metadata(page_id)` (cheap — no body) for
each, and compares versions. Any drift → the BRD is stale. Handled by
`fetch_confluence`, which re-embeds when `content_hash` changes.

**Fail-safe**: any HTTP / network error on the freshness check is treated as
"assume fresh" — a Confluence outage should not force full re-ingest of
every ticket.

---

## Trigger grid

Assume CDM-268 is already ingested (has `story_hash` + `bug_container_hashes`
in props).

| Event | Tier 1 | Tier 2 | Tier 3 | Effect |
|---|---|---|---|---|
| Nobody edited anything (user just chats) | ✅ hit | — | (checked in tier-1-hit path) | `cached_fresh`, ~500 ms |
| Someone views the ticket in Jira | ✅ hit | — | — | `cached_fresh` |
| Custom field changed | ✅ hit | — | — | `cached_fresh` — not tracked (by design) |
| Comment added on story | ✅ hit | — | — | `cached_fresh` — not tracked |
| Story status flip (In Progress → Done) | ❌ miss | ✅ runs | ✅ runs (via BRD check) | Full re-ingest |
| Assignee changed | ❌ miss | ✅ runs | ✅ runs | Full re-ingest |
| Subtask added / removed | ❌ miss (stubs[] changed) | ✅ runs | ✅ runs | Full re-ingest |
| Subtask status flip (Test on dev Done) | ❌ miss | ✅ runs | ✅ runs | Full re-ingest |
| Story description edited | ❌ miss | ✅ runs | ✅ runs | Full re-ingest |
| Confluence link added to description | ❌ miss (URLs changed) | ✅ runs | ✅ runs | Full re-ingest + new BRD |
| **Bug row added to `[Bug]` subtask table only** | ✅ hit (subtask summary/status unchanged) | — | — | **MISS — new row not ingested** until next story-level change or user forces refresh |
| Confluence PRD edited (Jira untouched) | ✅ hit | — | ❌ miss | Re-fetch that BRD only |
| `force=True` (user said "cập nhật") | Bypass | Bypass | Bypass | Full re-ingest |

---

## Force refresh — how the user triggers it

`slack_app._FORCE_REFRESH_RE` matches: `cập nhật`, `làm mới`, `đồng bộ`,
`refresh`, `resync`, `re-fetch`, `reload`, `mới nhất`. When any of these
appear in the user's message, the Slack layer calls
`ingest_jira_ticket(..., force=True)`.

That bypasses tier 1's short-circuit, forces tier 2 to re-parse every bug
container regardless of hash, and forces tier 3 to re-embed the BRD even
when `content_hash` matches (via `_check_brd_freshness` + `fetch_confluence`
seeing a changed `version` at Confluence side).

---

## Cost per event

Measured on live CDM-268 (Confluence page 55 KB, 1 `[Bug]` subtask with 5
rows, 4 TestRun subtasks, 1 linked BRD):

| Scenario | Latency | Where the time goes |
|---|---|---|
| Cold ingest (first time) | 4–15 s | Jira × ~6, Confluence × 1, LLM AC extract × 1 |
| Cached fresh (tier 1 hit) | ~500 ms | 1 Jira REST call + hash compare + Chroma noop |
| Story dirty, bug table unchanged (tier 2 hit) | ~1–3 s | +1 REST per `[Bug]` subtask, no re-parse |
| Story dirty, bug table changed (tier 2 miss) | ~2–5 s | +1 REST per `[Bug]` subtask + re-parse + N Bug upserts |
| BRD-drift (tier 3 miss) | +2–5 s | Full Confluence re-fetch + re-embed |
| `force=True` | 4–15 s | Same as cold ingest |

---

## Verify what's stored

```sql
docker exec tieu-kiwi-postgres-1 psql -U tieukiwi_app -d tieukiwi -c "
  SELECT ref,
         props_json->>'story_hash'           AS story_hash,
         props_json->'bug_container_hashes'  AS bug_hashes,
         props_json->>'jira_updated'         AS jira_updated,
         props_json->>'last_ingested_at'     AS last_ingested,
         props_json->>'last_seen_at'         AS last_seen
  FROM nodes
  WHERE type='Requirement' AND ref='CDM-268';
"
```

Every call to `ingest_jira_ticket` (even cached) updates `last_seen_at`, so
that field is a live signal: how recently was this ticket last asked about.
`last_ingested_at` moves only on actual full-ingest runs.

---

## Known gaps + workarounds

**Gap 1**: Bug-row-only edits without touching subtask summary/status miss
tier 1 → get treated as cached_fresh.

*Workaround*: user says "cập nhật CDM-268" (or any of the force-refresh
keywords) → bypasses all tiers.

*Fix option (deferred)*: lift tier 2 to top-level — fetch every `[Bug]`
subtask before tier 1 decides. Adds N REST calls per Slack message; the
cheap-check-first tradeoff has been kept.

**Gap 2**: Comment activity on Jira (which sometimes signals bugs / status)
is not hashed. If team convention starts putting decisions in comments,
add a `comments_hash` at tier 1.

**Gap 3**: If Jira's `updated` field ever becomes trustworthy for material
changes in your instance, it can be used as a cheap tie-breaker before
computing the story hash. Not currently used.

---

## Where to look in code

```
tieukiwi/jira_ingest.py
  _story_hash              tier 1 hash
  _bug_table_hash          tier 2 hash
  _check_brd_freshness     tier 3 drift check
  ingest_jira_ticket       check block near the top

tieukiwi/confluence.py
  get_page_metadata        tier 3 cheap version fetch
  fetch_confluence         re-embed when content_hash changes

tieukiwi/slack_app.py
  _FORCE_REFRESH_RE        keyword regex for force=True
  _ensure_ticket_fresh     pre-flight caller
```

Related: `docs/ROADMAP.md`, `docs/CHANGELOG.md`, and inline module
docstrings describe the surrounding pipeline.
