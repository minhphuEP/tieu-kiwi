-- Migration 007: normalize project_id + fix TestRun ref naming.
--
-- Two concerns handled in one migration (both derive from the same convention gap):
--
-- 1) project_id should equal the Jira project key: `CDM-199` → `CDM`.
--    Prior demo data used `CDM_TEAM`; `fetch_jira` was saving NULL. Unify to `CDM`.
--
-- 2) TestRun.ref must NOT collide with Requirement/Bug refs. Some ingest saved
--    TestRuns with `ref = <Jira-key>` (e.g. 'CDM-266'), which collides with the
--    Requirement of the same key under the (project_id, ref) unique index.
--    Convention: TestRun ref must be prefixed `TR-` (ideally `TR-<TMS-run-uuid>`,
--    but for legacy rows we use `TR-<jira-key>`).
--
-- Idempotent: re-running is safe (WHERE clauses filter rows already migrated).

BEGIN;

-- 1) Rename TestRun refs that shadow Requirement/Bug refs.
--    Must happen BEFORE step 3 (backfill NULL → CDM), otherwise the backfill
--    would try to set (CDM, CDM-266) on both the Requirement and the TestRun.
UPDATE nodes
   SET ref = 'TR-' || ref
 WHERE type = 'TestRun'
   AND ref ~ '^[A-Z]+-[0-9]+$'
   AND ref NOT LIKE 'TR-%';

-- 2) nodes: rename CDM_TEAM → CDM
UPDATE nodes
   SET project_id = 'CDM'
 WHERE project_id = 'CDM_TEAM';

-- 3) nodes: backfill NULL project_id from ref prefix (before first '-').
--    Scoped to CDM for now — broadens later when other Jira projects come online.
UPDATE nodes
   SET project_id = 'CDM'
 WHERE project_id IS NULL
   AND (
        ref ~ '^CDM-[0-9]+$'          -- Requirement / Bug parent key
     OR ref ~ '^CDM-[0-9]+-[0-9]+$'   -- Bug ref pattern: CDM-302-1
     OR ref ~ '^TR-CDM-'              -- TestRun after step 1
   );

-- 4) users: same rename (project-scoped routing)
UPDATE users
   SET project_id = 'CDM'
 WHERE project_id = 'CDM_TEAM';

-- 5) channel_project_map: rename channel bindings
UPDATE channel_project_map
   SET project_id = 'CDM', updated_at = now()
 WHERE project_id = 'CDM_TEAM';

COMMIT;
