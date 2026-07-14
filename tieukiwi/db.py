import logging
import os
import re
import psycopg
from contextlib import contextmanager

from .config import DATABASE_URL

_log = logging.getLogger(__name__)

_DATATABLE_COL_RE = re.compile(r"^datacol_?\d+$", re.IGNORECASE)


def is_datatable_testcase(props):
    """Infer whether a TestCase's props represent a data-driven table testcase.

    LLM-generated testcases (tieukiwi/testcase_gen.py) carry `data_variants`;
    Excel-ingested testcases (scripts/ingest/testcases.py) instead carry
    `raw_rows` with DataCol_N columns copied verbatim from the source sheet.
    Neither schema implies the other, so both must be checked.
    """
    if props.get("data_variants"):
        return True
    for row in props.get("raw_rows") or []:
        if isinstance(row, dict) and any(
            isinstance(k, str) and _DATATABLE_COL_RE.match(k.strip()) for k in row
        ):
            return True
    return False


_RENAME_HINT_MARKER = "← not used"


def _raw_rows_to_data_variants(raw_rows):
    """Recover data_variants from Excel-ingested `raw_rows`.

    Per kb/_global/QE/templates/testcase_template.md, a data-driven sheet's
    Section C is: a `DATA TABLE` separator, a `← not used` rename-hint row,
    then one data row per variant with Description + DataCol_N + Expected
    columns filled (Title/Priority/Pre-condition/Step_Description blank).
    scripts/ingest/testcases.py preserves all of this verbatim into `raw_rows`
    but never converts it to the `data_variants` shape testcase_gen.py and
    testcase_export.py expect — without this, an ingested DataTable testcase's
    real values never reach the LLM (or a re-export), leaving it looking
    like an empty table.
    """
    variants = []
    for row in raw_rows or []:
        if not isinstance(row, dict):
            continue
        if any(v == _RENAME_HINT_MARKER for v in row.values()):
            continue  # the rename-hint row itself, not a data variant
        if not any(isinstance(k, str) and _DATATABLE_COL_RE.match(k.strip()) for k in row):
            continue  # a plain step row, no DataCol_* columns
        variants.append({
            "label": row.get("Description", ""),
            "values": {k: v for k, v in row.items() if k != "Description"},
        })
    return variants

@contextmanager
def conn():
    if DATABASE_URL is None:
        raise RuntimeError(
            "DATABASE_URL is not set. Add it to .env (see .env.example) and run from the repo root."
        )
    with psycopg.connect(DATABASE_URL) as c:
        yield c

def add_node(type_, ref=None, props=None):
    with conn() as c:
        row = c.execute(
            "INSERT INTO nodes(type, ref, props_json) VALUES (%s,%s,%s) RETURNING id",
            (type_, ref, psycopg.types.json.Json(props or {})),
        ).fetchone()
        return row[0]

def add_edge(src_id, rel, dst_id, props=None):
    with conn() as c:
        c.execute(
            "INSERT INTO edges(src_id, rel, dst_id, props_json) VALUES (%s,%s,%s,%s)",
            (src_id, rel, dst_id, psycopg.types.json.Json(props or {})),
        )

# --- channel -> project resolution (Layer B glue) --------------------------

def project_for_channel(channel_id):
    """Return the project_id bound to a Slack channel, or None if unmapped.

    The Slack layer calls this at the entry point of every event handler:
      proj = db.project_for_channel(event["channel"]) or DEFAULT_PROJECT
      answer = agent.ask(text, project_id=proj)

    Populated via `channel_project_map` (migration 004). Use `bind_channel()`
    to insert/update mappings, or edit the table directly.
    """
    with conn() as c:
        row = c.execute(
            "SELECT project_id FROM channel_project_map WHERE channel_id=%s",
            (channel_id,),
        ).fetchone()
    return row[0] if row else None


def bind_channel(channel_id, project_id, team_id=None, note=None):
    """Upsert a channel -> project binding. Safe to re-run."""
    with conn() as c:
        c.execute(
            """
            INSERT INTO channel_project_map (channel_id, project_id, team_id, note, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (channel_id) DO UPDATE SET
              project_id = EXCLUDED.project_id,
              team_id    = COALESCE(EXCLUDED.team_id, channel_project_map.team_id),
              note       = COALESCE(EXCLUDED.note,    channel_project_map.note),
              updated_at = now()
            """,
            (channel_id, project_id, team_id, note),
        )


def resolve_role_slack_id(role, project_id=None):
    """Resolve a role name to a Slack user id via the users table.

    Project-scoped match first (when project_id is given), then any user with that role.
    Role matching is case-insensitive, so lowercase tokens (e.g. 'qe_lead') match the
    canonical upper-snake values stored in users.role ('QE_LEAD'). Returns None if no
    user with that role has a slack_id. Parameterized SQL.
    """
    with conn() as c:
        if project_id is not None:
            row = c.execute(
                "SELECT slack_id FROM users "
                "WHERE lower(role)=lower(%s) AND project_id=%s AND slack_id IS NOT NULL "
                "LIMIT 1",
                (role, project_id),
            ).fetchone()
            if row:
                return row[0]
        row = c.execute(
            "SELECT slack_id FROM users "
            "WHERE lower(role)=lower(%s) AND slack_id IS NOT NULL LIMIT 1",
            (role,),
        ).fetchone()
        return row[0] if row else None


def mention_for(role, project_id=None):
    """Resolve a role to a Slack mention string — the ONE mention path used everywhere.

    Order: users table (project-scoped first, then role-only) -> optional per-role env
    override ROLE_<ROLE> (last resort, so demos don't break on an empty table) -> a
    clear, non-crashing "@<role> (unconfigured)" label (logs a warning). Never raises.
    """
    sid = None
    try:
        sid = resolve_role_slack_id(role, project_id)
    except Exception:
        _log.exception("resolve_role_slack_id failed for role=%s", role)
    if not sid:
        sid = os.getenv(f"ROLE_{role.upper()}")  # e.g. ROLE_DELIVERY_MANAGER
    if sid:
        return f"<@{sid}>"
    _log.warning("No Slack user for role '%s' (project=%s); mention unconfigured", role, project_id)
    return f"@{role} (unconfigured)"


def upsert_node_by_ref(type_, ref, props=None, project_id=None, merge_props=False):
    """Insert a node, or update its props if one with the same (type, ref) exists.

    Avoids duplicates when re-fetching the same external item (e.g. a Jira issue).
    Prefer the node with 'has' edges (the one linked to ACs) when duplicates
    exist, so re-fetch updates the AC-bearing node rather than an empty duplicate.

    Args:
      type_:       node type (e.g. 'Requirement', 'BRD').
      ref:         external ref (e.g. 'CDM-268').
      props:       props dict; None/omitted → empty {}.
      project_id:  scope the upsert to a project. Nodes with the same ref in a
                   DIFFERENT project stay separate. Default None matches ANY
                   project — legacy behaviour.
      merge_props: True → merge new props into existing via jsonb `||`,
                   preserving unspecified keys. Default False → replace props
                   wholesale (matches pre-phuong_qe behaviour of this file).

    Uses the partial unique index (project_id, ref) WHERE ref IS NOT NULL from
    migration 003 for ON CONFLICT-based idempotent upsert when project_id is
    given; falls back to SELECT-then-INSERT/UPDATE for legacy project_id=None
    calls (prefer-has-edges heuristic).
    """
    props = props or {}
    with conn() as c:
        if project_id is None:
            # Legacy path: no project scope → prefer-has-edges SELECT then INSERT/UPDATE.
            row = c.execute(
                """
                SELECT n.id FROM nodes n
                WHERE n.type=%s AND n.ref=%s
                ORDER BY (SELECT count(*) FROM edges e WHERE e.src_id=n.id AND e.rel='has') DESC,
                         n.id ASC
                LIMIT 1
                """,
                (type_, ref),
            ).fetchone()
            if row:
                node_id = row[0]
                if merge_props:
                    c.execute(
                        "UPDATE nodes SET props_json = COALESCE(props_json,'{}'::jsonb) || %s::jsonb WHERE id=%s",
                        (psycopg.types.json.Json(props), node_id),
                    )
                else:
                    c.execute(
                        "UPDATE nodes SET props_json=%s WHERE id=%s",
                        (psycopg.types.json.Json(props), node_id),
                    )
                return node_id
            row = c.execute(
                "INSERT INTO nodes(type, ref, props_json) VALUES (%s,%s,%s) RETURNING id",
                (type_, ref, psycopg.types.json.Json(props)),
            ).fetchone()
            return row[0]

        # Multi-tenant path: use ON CONFLICT with the (project_id, ref) unique index
        conflict_expr = (
            "props_json = nodes.props_json || EXCLUDED.props_json" if merge_props
            else "props_json = EXCLUDED.props_json"
        )
        row = c.execute(
            f"""
            INSERT INTO nodes (type, ref, project_id, props_json)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (project_id, ref) WHERE ref IS NOT NULL DO UPDATE
              SET {conflict_expr}
            RETURNING id
            """,
            (type_, ref, project_id, psycopg.types.json.Json(props)),
        ).fetchone()
        return row[0]


def get_node_by_ref(type_, ref, project_id=None):
    """Return {id, props_json} for the node, or None. Used to check existing
    content_hash before re-embedding a Confluence page, etc."""
    with conn() as c:
        sql = "SELECT id, props_json FROM nodes WHERE type=%s AND ref=%s"
        params = [type_, ref]
        if project_id is not None:
            sql += " AND project_id=%s"
            params.append(project_id)
        row = c.execute(sql + " LIMIT 1", params).fetchone()
        if not row:
            return None
        return {"id": row[0], "props_json": row[1] or {}}


def count_acs(req_node_id):
    """Return the number of AcceptanceCriterion nodes a Requirement has.

    Used by ingest to decide whether to re-run AC extraction when the linked
    BRD's content is unchanged (status='cached') — normally we skip in that
    case, but if AC count is 0 the previous extraction never persisted
    anything (LLM error, section-slice miss, etc.) and we should retry.
    """
    with conn() as c:
        row = c.execute(
            "SELECT count(*) FROM nodes ac "
            "JOIN edges e ON e.dst_id=ac.id AND e.rel='has' "
            "WHERE e.src_id=%s AND ac.type='AcceptanceCriterion'",
            (req_node_id,),
        ).fetchone()
        return row[0] if row else 0


def count_acs_by_anchor(req_node_id, section_anchor):
    """Return AC count for a Requirement scoped to one BRD section anchor.

    Same semantics as count_acs() but keyed on props_json->>'section_anchor'.
    Lets ingest gate per (page, anchor) instead of per-Requirement — needed
    when one Requirement links to multiple sections of the SAME BRD page
    (URL#14 + URL#15 case): the second section's extraction must not be
    skipped just because the first section already produced ACs.

    When section_anchor is None, falls back to counting anchor-less ACs
    (whole-doc extractions).
    """
    with conn() as c:
        if section_anchor is None:
            row = c.execute(
                "SELECT count(*) FROM nodes ac "
                "JOIN edges e ON e.dst_id=ac.id AND e.rel='has' "
                "WHERE e.src_id=%s AND ac.type='AcceptanceCriterion' "
                "AND (ac.props_json->>'section_anchor') IS NULL",
                (req_node_id,),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT count(*) FROM nodes ac "
                "JOIN edges e ON e.dst_id=ac.id AND e.rel='has' "
                "WHERE e.src_id=%s AND ac.type='AcceptanceCriterion' "
                "AND ac.props_json->>'section_anchor' = %s",
                (req_node_id, section_anchor),
            ).fetchone()
        return row[0] if row else 0


def linked_brds(src_id):
    """Return BRD nodes reachable via `src -derivedFrom-> BRD`.

    Each item: {id, ref, props_json}. Used by the ingest hash-gate to check
    whether any Confluence page a Requirement derived from has drifted
    version-wise (i.e. the PRD was edited without touching Jira).
    """
    with conn() as c:
        rows = c.execute(
            "SELECT n.id, n.ref, n.props_json FROM nodes n "
            "JOIN edges e ON e.dst_id=n.id AND e.rel='derivedFrom' "
            "WHERE e.src_id=%s AND n.type='BRD'",
            (src_id,),
        ).fetchall()
    return [{"id": r[0], "ref": r[1], "props_json": r[2] or {}} for r in rows]

# ---- Polymorphic get_ticket + view helpers (phuong_qe add) ----

# Jira-style ref regex, e.g. 'CDM-268', 'FRONT-3494'. Group 1 = project code.
_JIRA_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]*)-\d+")
def _now_utc_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()



def project_from_ref(ref):
    """Derive project_id from a Jira-style ref: 'CDM-199' -> 'CDM'.

    Convention (docs/GRAPH_INSERT_GUIDE.md): project_id equals the prefix before
    the first '-' in a ref. Also matches nested refs like 'CDM-302-1' (Bug).
    Returns None if the ref doesn't match a Jira key pattern.
    """
    if not ref:
        return None
    m = _JIRA_KEY_RE.match(ref)
    return m.group(1) if m else None


def _fetch_node_polymorphic(c, ref, project_id=None):
    """Find a node by ref with a TR-<ref> fallback (migration 007 convention).

    Slack users type 'CDM-263' but TestRun refs are stored as 'TR-CDM-263'.
    """
    def _one(target_ref):
        sql = "SELECT id, type, ref, project_id, props_json FROM nodes WHERE ref=%s"
        params = [target_ref]
        if project_id is not None:
            sql += " AND project_id=%s"
            params.append(project_id)
        sql += " ORDER BY id LIMIT 1"
        return c.execute(sql, params).fetchone()

    row = _one(ref)
    if not row and _JIRA_KEY_RE.match(ref or "") and not ref.startswith("TR-"):
        row = _one(f"TR-{ref}")
    return row


def _view_requirement(c, node_id, ref, props):
    """Return AC list (with coverage), linked BRDs, parent story, warnings."""
    us = c.execute(
        "SELECT n.ref, n.props_json FROM nodes n "
        "JOIN edges e ON e.src_id=n.id AND e.rel='has' "
        "WHERE e.dst_id=%s AND n.type='UserStory' LIMIT 1",
        (node_id,),
    ).fetchone()
    user_story = {"ref": us[0], "title": (us[1] or {}).get("title")} if us else None

    brds = [
        {
            "ref": r[0], "title": (r[1] or {}).get("title"),
            "url": (r[1] or {}).get("url") or (r[1] or {}).get("_meta", {}).get("source_file"),
            "section_anchor": (r[1] or {}).get("section_anchor"),
            "content_preview": (r[1] or {}).get("content_preview"),
        }
        for r in c.execute(
            "SELECT n.ref, n.props_json FROM nodes n "
            "JOIN edges e ON e.dst_id=n.id AND e.rel='derivedFrom' "
            "WHERE e.src_id=%s AND n.type='BRD' ORDER BY n.ref",
            (node_id,),
        ).fetchall()
    ]

    acs = []
    for ac_id, ac_ref, ac_props in c.execute(
        "SELECT ac.id, ac.ref, ac.props_json FROM nodes ac "
        "JOIN edges e ON e.dst_id=ac.id AND e.rel='has' "
        "WHERE e.src_id=%s AND ac.type='AcceptanceCriterion' ORDER BY ac.ref",
        (node_id,),
    ).fetchall():
        # Pull the TC props alongside its ref so the caller (LLM / Slack) can
        # render title + review_status + priority without a follow-up call.
        # Legacy TCs without review_status get COALESCE'd to 'qe_reviewed'
        # (same rule as strict-coverage in _uncovered_acs).
        tc_rows = c.execute(
            "SELECT tc.ref, tc.props_json FROM nodes tc "
            "JOIN edges cov ON cov.dst_id=tc.id AND cov.rel='coveredBy' "
            "WHERE cov.src_id=%s AND tc.type='TestCase' ORDER BY tc.ref",
            (ac_id,),
        ).fetchall()
        testcases = []
        for tc_ref, tc_props in tc_rows:
            tp = tc_props or {}
            testcases.append({
                "ref": tc_ref,
                "title": tp.get("title"),
                "review_status": tp.get("review_status") or "qe_reviewed",
                "priority": tp.get("priority"),
            })
        p = ac_props or {}
        acs.append({
            "ref": ac_ref, "title": p.get("title"),
            "detail": p.get("detail") or p.get("desc"),
            "coverage": {
                "has_testcase": bool(testcases),
                "testcases": testcases,
                # Legacy: bare-ref list kept for any older caller that reads it.
                "testcase_refs": [t["ref"] for t in testcases],
            },
        })

    # TestRun subtasks of this Requirement — linked via props.jira_parent_ref
    # (not by edge; convention in jira_ingest since the parent link is Jira-native).
    test_runs = [
        {
            "ref": r[0],
            "title": (r[1] or {}).get("title") or (r[1] or {}).get("summary"),
            "environment": (r[1] or {}).get("environment"),
            "status": (r[1] or {}).get("status"),
        }
        for r in c.execute(
            "SELECT ref, props_json FROM nodes "
            "WHERE type='TestRun' AND props_json->>'jira_parent_ref'=%s ORDER BY ref",
            (ref,),
        ).fetchall()
    ]

    # Bugs linked to this Requirement — via props.jira_parent_ref (covers
    # bugs materialised from a [Bug]-subtask table under this Story).
    linked_bugs = [
        {
            "ref": r[0],
            "title": (r[1] or {}).get("title") or (r[1] or {}).get("summary"),
            "severity": (r[1] or {}).get("severity"),
            "status": (r[1] or {}).get("status"),
            "find_by": (r[1] or {}).get("find_by"),
        }
        for r in c.execute(
            "SELECT ref, props_json FROM nodes "
            "WHERE type='Bug' AND props_json->>'jira_parent_ref'=%s ORDER BY ref",
            (ref,),
        ).fetchall()
    ]

    warnings = []
    if not acs:
        warnings.append(
            "0 acceptance criteria trong graph. PRD chưa được extract thành AC — "
            "gọi ingest_jira_ticket với extract_acs=True. TUYỆT ĐỐI KHÔNG bịa AC."
        )
    else:
        def _ac_label(a):
            # Prefer human-readable desc/title over the opaque hash ref
            # (AC-CDM-268-0d2262c6). Fall back to ref if neither present.
            txt = (a.get("detail") or a.get("title") or "").strip()
            if not txt:
                return a["ref"]
            txt = txt.split("\n", 1)[0]
            return txt if len(txt) <= 80 else txt[:77] + "…"

        uncovered_items = [a for a in acs if not a["coverage"]["has_testcase"]]
        if uncovered_items:
            labels = [f"'{_ac_label(a)}'" for a in uncovered_items]
            warnings.append(
                f"{len(uncovered_items)}/{len(acs)} AC chưa có TestCase: "
                f"{', '.join(labels)}. Attach TC lên Jira task hoặc chạy gen_testcase."
            )
    if not brds:
        warnings.append("Không có BRD/PRD link. Nếu Jira desc có Confluence URL, gọi ingest_jira_ticket.")
    if not test_runs:
        warnings.append(
            "Chưa có TestRun subtask nào. QE có thể chưa tạo subtask '[QE] testing on beta/prod' "
            "trên Jira, hoặc chạy ingest_jira_ticket để pull subtree."
        )
    open_critical = [b for b in linked_bugs if b["status"] not in ("done", "closed") and b["severity"] in ("critical", "high")]
    if open_critical:
        warnings.append(
            f"{len(open_critical)} critical/high bug đang open: "
            f"{', '.join(b['ref'] for b in open_critical)}. Cần fix trước khi go-live."
        )

    return {
        "ref": ref, "type": "Requirement", "found": True, "props": props,
        "user_story": user_story, "brds": brds,
        "acceptance_criteria": acs,
        "test_runs": test_runs,
        "linked_bugs": linked_bugs,
        "warnings": warnings,
    }


def _view_bug(c, node_id, ref, props):
    """Return severity, affected components, violated ACs, and which TestRun found it."""
    affects = [r[0] for r in c.execute(
        "SELECT n.ref FROM nodes n JOIN edges e ON e.dst_id=n.id "
        "WHERE e.src_id=%s AND e.rel='affects' AND n.type='Component'",
        (node_id,),
    ).fetchall()]
    violates = [r[0] for r in c.execute(
        "SELECT n.ref FROM nodes n JOIN edges e ON e.dst_id=n.id "
        "WHERE e.src_id=%s AND e.rel='violates' AND n.type='AcceptanceCriterion'",
        (node_id,),
    ).fetchall()]
    found_by = [r[0] for r in c.execute(
        "SELECT n.ref FROM nodes n JOIN edges e ON e.src_id=n.id "
        "WHERE e.dst_id=%s AND e.rel='finds' AND n.type='TestRun'",
        (node_id,),
    ).fetchall()]
    warnings = []
    if not violates:
        warnings.append("Bug này chưa link tới AC nào (edge `violates`). Traceability thiếu.")
    if not found_by:
        warnings.append("Bug chưa link tới TestRun (edge `finds`) — có thể leaked to production.")
    return {
        "ref": ref, "type": "Bug", "found": True, "props": props,
        "affects_components": affects, "violates_acs": violates,
        "found_by_testruns": found_by, "warnings": warnings,
    }


def _view_testrun(c, node_id, ref, props):
    """Return linked TestCase (via executedBy) and bugs found."""
    tc = c.execute(
        "SELECT n.ref, n.props_json FROM nodes n JOIN edges e ON e.src_id=n.id "
        "WHERE e.dst_id=%s AND e.rel='executedBy' AND n.type='TestCase' LIMIT 1",
        (node_id,),
    ).fetchone()
    testcase = {"ref": tc[0], "title": (tc[1] or {}).get("title")} if tc else None
    bugs = [
        {"ref": r[0], "severity": (r[1] or {}).get("severity"), "status": (r[1] or {}).get("status")}
        for r in c.execute(
            "SELECT n.ref, n.props_json FROM nodes n JOIN edges e ON e.dst_id=n.id "
            "WHERE e.src_id=%s AND e.rel='finds' AND n.type='Bug' ORDER BY n.ref",
            (node_id,),
        ).fetchall()
    ]
    warnings = []
    if not testcase:
        warnings.append("TestRun chưa link tới TestCase (edge `executedBy`). Đóng góp coverage = 0.")
    return {
        "ref": ref, "type": "TestRun", "found": True, "props": props,
        "testcase": testcase, "bugs_found": bugs, "warnings": warnings,
    }


def _view_userstory_or_epic(c, node_id, ref, props):
    """Epic/UserStory view: list linked Requirements + aggregate TestRuns/Bugs
    across all children (via jira_parent_ref chain)."""
    reqs = [
        {"ref": r[0], "title": (r[1] or {}).get("title")}
        for r in c.execute(
            "SELECT n.ref, n.props_json FROM nodes n JOIN edges e ON e.dst_id=n.id "
            "WHERE e.src_id=%s AND e.rel='has' AND n.type='Requirement' ORDER BY n.ref",
            (node_id,),
        ).fetchall()
    ]

    # Aggregate TestRuns of all child Requirements — bằng cách join qua
    # jira_parent_ref của TestRun (parent = child Requirement.ref, không phải Epic).
    child_refs = [r["ref"] for r in reqs]
    test_runs = []
    linked_bugs = []
    if child_refs:
        test_runs = [
            {
                "ref": r[0],
                "title": (r[1] or {}).get("title") or (r[1] or {}).get("summary"),
                "environment": (r[1] or {}).get("environment"),
                "status": (r[1] or {}).get("status"),
                "parent_story": (r[1] or {}).get("jira_parent_ref"),
            }
            for r in c.execute(
                "SELECT ref, props_json FROM nodes "
                "WHERE type='TestRun' AND props_json->>'jira_parent_ref' = ANY(%s) ORDER BY ref",
                (child_refs,),
            ).fetchall()
        ]
        linked_bugs = [
            {
                "ref": r[0],
                "title": (r[1] or {}).get("title") or (r[1] or {}).get("summary"),
                "severity": (r[1] or {}).get("severity"),
                "status": (r[1] or {}).get("status"),
                "parent_story": (r[1] or {}).get("jira_parent_ref"),
            }
            for r in c.execute(
                "SELECT ref, props_json FROM nodes "
                "WHERE type='Bug' AND props_json->>'jira_parent_ref' = ANY(%s) ORDER BY ref",
                (child_refs,),
            ).fetchall()
        ]

    warnings = [] if reqs else ["Epic/Story chưa có Requirement con nào."]
    return {
        "ref": ref, "type": "UserStory", "found": True, "props": props,
        "requirements": reqs,
        "test_runs": test_runs,
        "linked_bugs": linked_bugs,
        "warnings": warnings,
    }


def _view_brd(c, node_id, ref, props):
    """BRD view: preview + downstream requirements."""
    downstream = [r[0] for r in c.execute(
        "SELECT n.ref FROM nodes n JOIN edges e ON e.src_id=n.id "
        "WHERE e.dst_id=%s AND e.rel='derivedFrom' AND n.type='Requirement' ORDER BY n.ref",
        (node_id,),
    ).fetchall()]
    return {
        "ref": ref, "type": "BRD", "found": True, "props": props,
        "downstream_requirements": downstream, "warnings": [],
    }


def get_ticket(ref, project_id=None):
    """Polymorphic read: dispatch view by node.type. This is the READ entry point
    the agent should call first when a user mentions a ticket key (`CDM-XXX`).

    Smart lookup: falls back to `TR-<ref>` if the direct ref misses (matches the
    convention from migration 007 for TestRun subtasks).

    Returns `{ref, type, found, props, ...type-specific fields..., warnings}`.
    When `warnings` is non-empty, the agent MUST echo each to the user — they
    flag missing data / missing edges the user needs to know about.

    See docs/GRAPH_INSERT_GUIDE.md for node type conventions.
    """
    with conn() as c:
        row = _fetch_node_polymorphic(c, ref, project_id=project_id)
        if not row:
            return {
                "ref": ref, "found": False,
                "warnings": [
                    f"Ticket {ref} không có trong graph"
                    + (f" (project_id={project_id})" if project_id else "")
                    + ". Gọi ingest_jira_ticket để pull từ Jira."
                ],
            }
        node_id, n_type, node_ref, _, props = row
        props = props or {}
        # Legacy: some pipelines wrote `summary` instead of `title`. Surface both.
        if n_type in ("Requirement", "UserStory") and not props.get("title") and props.get("summary"):
            props = {**props, "title": props["summary"]}

        if n_type == "Requirement":
            return _view_requirement(c, node_id, node_ref, props)
        if n_type == "UserStory":
            return _view_userstory_or_epic(c, node_id, node_ref, props)
        if n_type == "Bug":
            return _view_bug(c, node_id, node_ref, props)
        if n_type == "TestRun":
            return _view_testrun(c, node_id, node_ref, props)
        if n_type == "BRD":
            return _view_brd(c, node_id, node_ref, props)
        # Default fallback (TestCase, AC, Component, Sprint, Task, ...)
        return {
            "ref": node_ref, "type": n_type, "found": True, "props": props,
            "warnings": [],
        }






def mark_reviewed(tc_ref, decision, reviewer_slack_id, comments=None, project_id=None):
    """Advance a TestCase through the review state machine.

    Args:
      tc_ref:             TestCase ref (e.g. 'CDM_DupScript_002').
      decision:           'approve' or 'reject'.
      reviewer_slack_id:  Slack user id doing the review (recorded per stage).
      comments:           optional free-text comment (recorded on the transition).
      project_id:         multi-tenant guard. The TC must belong to this project.

    Returns dict:
      {status: 'ok'|'error'|'terminal', old_state, new_state, tc_ref, reasoning}

    Non-destructive: on error (unknown TC, invalid transition, terminal state)
    props stay untouched.
    """
    if decision not in ("approve", "reject"):
        return {"status": "error", "tc_ref": tc_ref,
                "reasoning": f"decision must be 'approve' or 'reject', got {decision!r}"}

    tc = get_node_by_ref("TestCase", tc_ref, project_id=project_id)
    if not tc:
        scope = f" in project '{project_id}'" if project_id else ""
        return {"status": "error", "tc_ref": tc_ref,
                "reasoning": f"TestCase '{tc_ref}' not found{scope}"}

    props = tc["props_json"] or {}
    old_state = props.get("review_status") or "draft"
    transitions = _REVIEW_STATE_TRANSITIONS.get(old_state, {})
    if not transitions:
        return {"status": "terminal", "tc_ref": tc_ref, "old_state": old_state,
                "new_state": old_state,
                "reasoning": f"TestCase already in terminal state '{old_state}'"}

    new_state = transitions.get(decision)
    if new_state is None:
        return {"status": "error", "tc_ref": tc_ref, "old_state": old_state,
                "reasoning": f"decision '{decision}' invalid from state '{old_state}'"}

    now = _now_utc_iso()
    # Record reviewer + timestamp on the STAGE, so the audit trail persists
    # even after the state advances again.
    stage_key = {
        ("draft", "approve"):        ("qe_started_at",   "qe_started_by"),
        ("qe_pending", "approve"):   ("qe_reviewed_at",  "reviewed_by_qe"),
        ("qe_pending", "reject"):    ("qe_rejected_at",  "rejected_by_qe"),
        ("qe_reviewed", "approve"):  ("lead_started_at", "lead_started_by"),
        ("lead_pending", "approve"): ("lead_approved_at","reviewed_by_qe_lead"),
        ("lead_pending", "reject"):  ("lead_rejected_at","rejected_by_qe_lead"),
        ("draft", "reject"):         ("draft_rejected_at", "rejected_by"),
        ("qe_reviewed", "reject"):   ("qe_rejected_at",    "rejected_by"),
    }.get((old_state, decision))
    delta = {"review_status": new_state}
    if stage_key:
        delta[stage_key[0]] = now
        delta[stage_key[1]] = reviewer_slack_id
    if comments:
        history = list(props.get("review_history") or [])
        history.append({
            "at": now, "from": old_state, "to": new_state,
            "by": reviewer_slack_id, "comment": comments,
        })
        delta["review_history"] = history

    upsert_node_by_ref("TestCase", tc_ref, delta,
                        project_id=project_id, merge_props=True)

    return {
        "status": "ok", "tc_ref": tc_ref,
        "old_state": old_state, "new_state": new_state,
        "reviewer": reviewer_slack_id,
        "reasoning": f"TestCase {tc_ref}: {old_state} → {new_state}",
    }



# ---- Code-graph impact analysis (phuong_qe add) ----

def feature_blast_radius(component_ref, project_id=None):
    """Impact analysis starting from a Component (feature), not from a diff.

    Answer: "If feature X is being developed, what OTHER features are at
    risk of breaking?" Also lists Requirements + ACs + TestCases scoped to
    the affected features so QE knows what to plan for.

    Walk:
      - dependents:   Components that dependsOn the target (they'd break
                      if target breaks; ranked HIGH).
      - target itself is the "you-are-here" (HIGH).
      - Requirements: any Requirement impacting the target OR a dependent
                      Component. Severity = max Component severity impacted.
      - ACs:          under those Requirements. Inherit severity.
      - TestCases:    coveredBy those ACs. Inherit severity.

    Unlike code_impact (which walks the code closure), this is pure
    business-graph traversal — no code awareness. Use it when you know the
    feature but haven't cut code yet, or want to answer questions like
    "what should QE plan for CDM-268?".
    """
    _sev_rank = {"high": 3, "medium": 2, "low": 1}
    with conn() as c:
        # Resolve target Component
        sql = "SELECT id, ref, props_json->>'name' FROM nodes WHERE type='Component' AND ref=%s"
        params = [component_ref]
        if project_id is not None:
            sql += " AND project_id=%s"; params.append(project_id)
        row = c.execute(sql, params).fetchone()
        if not row:
            return {"target": {"ref": component_ref, "name": None},
                    "affected_components": [], "affected_requirements": [],
                    "affected_acs": [], "affected_testcases": [],
                    "severity_counts": {"components": {"high":0,"medium":0,"low":0},
                                        "requirements": {"high":0,"medium":0,"low":0},
                                        "acs":          {"high":0,"medium":0,"low":0},
                                        "testcases":    {"high":0,"medium":0,"low":0}},
                    "warning": f"No Component found with ref={component_ref}"}
        target_id, target_ref, target_name = row

        # Walk dependents (Components -dependsOn-> target, transitively)
        rows = c.execute(
            """
            WITH RECURSIVE dep_closure(comp_id, d) AS (
              SELECT %s::bigint, 0
              UNION
              SELECT e.src_id, dc.d + 1
                FROM dep_closure dc
                JOIN edges e ON e.dst_id = dc.comp_id AND e.rel = 'dependsOn'
               WHERE dc.d < 5
            )
            SELECT DISTINCT n.id, n.ref, n.props_json->>'name', MIN(dc.d)
              FROM dep_closure dc
              JOIN nodes n ON n.id = dc.comp_id
             WHERE n.type = 'Component'
                   """ + ("AND n.project_id = %s" if project_id else "") + """
             GROUP BY n.id, n.ref, n.props_json->>'name'
             ORDER BY MIN(dc.d), n.ref
            """,
            ([target_id, project_id] if project_id else [target_id]),
        ).fetchall()
        all_comp = {}
        for cid, cref, cname, dep_d in rows:
            all_comp[cid] = {
                "id": cid, "ref": cref, "name": cname,
                # Target itself and its DIRECT dependents (dep_d==1) get HIGH.
                # More distant dependents get MEDIUM (they may or may not break).
                "severity": ("high" if dep_d <= 1 else "medium"),
                "via":      ("target" if dep_d == 0 else "dependsOn"),
                "dep_depth": dep_d,
            }

        # Requirements impacting ANY affected Component
        comp_ids = list(all_comp.keys())
        rows = c.execute(
            """
            SELECT r.id, r.ref, r.props_json->>'title', e.dst_id
              FROM nodes r
              JOIN edges e ON e.src_id = r.id AND e.rel = 'impacts' AND e.dst_id = ANY(%s)
             WHERE r.type = 'Requirement'
                   """ + ("AND r.project_id = %s" if project_id else "") + """
            """,
            ([comp_ids, project_id] if project_id else [comp_ids]),
        ).fetchall()
        reqs_by_id = {}
        for rid, rref, rtitle, comp_id in rows:
            entry = reqs_by_id.setdefault(rid, {
                "id": rid, "ref": rref, "title": rtitle,
                "impacted_components": [], "severity": "low",
            })
            csev = all_comp[comp_id]["severity"]
            entry["impacted_components"].append(all_comp[comp_id]["ref"])
            if _sev_rank[csev] > _sev_rank[entry["severity"]]:
                entry["severity"] = csev

        # ACs under those Requirements
        acs_by_id = {}
        if reqs_by_id:
            req_ids = list(reqs_by_id.keys())
            sev_by_req = {r["id"]: r["severity"] for r in reqs_by_id.values()}
            ref_by_req = {r["id"]: r["ref"]      for r in reqs_by_id.values()}
            rows = c.execute(
                """
                SELECT a.id, a.ref, a.props_json->>'title', e.src_id
                  FROM nodes a
                  JOIN edges e ON e.dst_id = a.id AND e.rel = 'has' AND e.src_id = ANY(%s)
                 WHERE a.type = 'AcceptanceCriterion'
                       """ + ("AND a.project_id = %s" if project_id else "") + """
                """,
                ([req_ids, project_id] if project_id else [req_ids]),
            ).fetchall()
            for aid, aref, atitle, rid in rows:
                acs_by_id.setdefault(aid, {
                    "id": aid, "ref": aref, "title": atitle,
                    "severity": sev_by_req[rid],
                    "parent_requirement": ref_by_req[rid],
                })

        # TestCases covering those ACs
        tcs_by_id = {}
        if acs_by_id:
            ac_ids = list(acs_by_id.keys())
            sev_by_ac = {a["id"]: a["severity"] for a in acs_by_id.values()}
            ref_by_ac = {a["id"]: a["ref"]      for a in acs_by_id.values()}
            rows = c.execute(
                """
                SELECT t.id, t.ref, t.props_json->>'title', t.props_json->>'priority', e.src_id
                  FROM nodes t
                  JOIN edges e ON e.dst_id = t.id AND e.rel = 'coveredBy' AND e.src_id = ANY(%s)
                 WHERE t.type = 'TestCase'
                       """ + ("AND t.project_id = %s" if project_id else "") + """
                """,
                ([ac_ids, project_id] if project_id else [ac_ids]),
            ).fetchall()
            for tid, tref, ttitle, tprio, ac_id in rows:
                entry = tcs_by_id.setdefault(tid, {
                    "id": tid, "ref": tref, "title": ttitle,
                    "priority": tprio,
                    "severity": sev_by_ac[ac_id],
                    "covers_acs": [],
                })
                entry["covers_acs"].append(ref_by_ac[ac_id])
                asev = sev_by_ac[ac_id]
                if _sev_rank[asev] > _sev_rank[entry["severity"]]:
                    entry["severity"] = asev

    def _sev_key(x): return (-_sev_rank[x["severity"]], x.get("ref", ""))
    comp_list = sorted(all_comp.values(), key=_sev_key)
    req_list  = sorted(reqs_by_id.values(), key=_sev_key)
    ac_list   = sorted(acs_by_id.values(), key=_sev_key)
    tc_list   = sorted(tcs_by_id.values(), key=_sev_key)
    def _strip(x): return {k: v for k, v in x.items() if k != "id"}

    return {
        "target": {"ref": target_ref, "name": target_name},
        "affected_components":   [_strip(v) for v in comp_list],
        "affected_requirements": [_strip(r) for r in req_list],
        "affected_acs":          [_strip(a) for a in ac_list],
        "affected_testcases":    [_strip(t) for t in tc_list],
        "severity_counts": {
            "components":   {s: sum(1 for v in comp_list if v["severity"] == s) for s in ("high","medium","low")},
            "requirements": {s: sum(1 for r in req_list  if r["severity"] == s) for s in ("high","medium","low")},
            "acs":          {s: sum(1 for a in ac_list   if a["severity"] == s) for s in ("high","medium","low")},
            "testcases":    {s: sum(1 for t in tc_list   if t["severity"] == s) for s in ("high","medium","low")},
        },
    }


def impact_from_jira(jira_ref, project_id=None):
    """Impact analysis starting from a Jira ticket (Requirement), aggregating
    over every Component the Requirement declares `impacts` on.

    Answers: "For CDM-268, what's the total QE blast radius?" Walks:
      - target Components:    every Component `impacts`-linked to the Requirement.
      - at-risk Components:   Components that `dependsOn` any target (transitively).
      - at-risk Requirements: OTHER Requirements impacting ANY Component in the
                              closure — signals cross-feature regression risk.
      - ACs / TestCases:      self ACs + at-risk req ACs + their TCs.

    Severity:
      - target Components:               HIGH
      - direct dependents (dep=1):       HIGH
      - transitive dependents (dep>1):   MEDIUM
      - Requirements/ACs/TCs:            inherit MAX severity of Components they touch;
                                         self-Req + its ACs pinned HIGH.

    Contract with `impacts_map.yml`: this tool is USELESS if the Requirement has
    no `impacts` edges. Returns a `warning` in that case so the caller (agent /
    Slack) can prompt for map seeding instead of silently returning empty.
    """
    _sev_rank = {"high": 3, "medium": 2, "low": 1}
    with conn() as c:
        sql = "SELECT id, ref, props_json->>'title' FROM nodes WHERE type='Requirement' AND ref=%s"
        params = [jira_ref]
        if project_id is not None:
            sql += " AND project_id=%s"; params.append(project_id)
        row = c.execute(sql, params).fetchone()
        if not row:
            return {"requirement": {"ref": jira_ref, "title": None},
                    "target_components": [], "at_risk_components": [],
                    "at_risk_requirements": [], "affected_acs": [],
                    "affected_testcases": [], "self_coverage_gap": [],
                    "severity_counts": {"components": {"high":0,"medium":0,"low":0},
                                        "requirements": {"high":0,"medium":0,"low":0},
                                        "acs":          {"high":0,"medium":0,"low":0},
                                        "testcases":    {"high":0,"medium":0,"low":0}},
                    "warning": f"No Requirement found with ref={jira_ref}"
                               + (f" in project {project_id}" if project_id else "")}
        req_id, req_ref, req_title = row

        # Target Components — every `impacts`-linked Component of this Requirement.
        rows = c.execute(
            """
            SELECT n.id, n.ref, n.props_json->>'name'
              FROM edges e
              JOIN nodes n ON n.id = e.dst_id
             WHERE e.src_id = %s AND e.rel = 'impacts' AND n.type = 'Component'
                   """ + ("AND n.project_id = %s" if project_id else "") + """
             ORDER BY n.ref
            """,
            ([req_id, project_id] if project_id else [req_id]),
        ).fetchall()
        target_comp_ids = [r[0] for r in rows]
        target_comp_meta = {r[0]: {"ref": r[1], "name": r[2]} for r in rows}

        if not target_comp_ids:
            # Bail early with a directive warning so the caller can guide the user.
            return {"requirement": {"ref": req_ref, "title": req_title},
                    "target_components": [], "at_risk_components": [],
                    "at_risk_requirements": [], "affected_acs": [],
                    "affected_testcases": [], "self_coverage_gap": [],
                    "severity_counts": {"components": {"high":0,"medium":0,"low":0},
                                        "requirements": {"high":0,"medium":0,"low":0},
                                        "acs":          {"high":0,"medium":0,"low":0},
                                        "testcases":    {"high":0,"medium":0,"low":0}},
                    "warning": (f"Requirement {req_ref} has no `impacts` edges. "
                                f"Seed data_ingestion/impacts_map.yml with the Components "
                                f"this Requirement scopes, then run "
                                f"`python scripts/ingest/impacts.py` and retry.")}

        # dep_closure — walk INCOMING dependsOn edges from every target (multi-seed).
        # Result: {comp_id: min_dep_depth_from_any_target}
        rows = c.execute(
            """
            WITH RECURSIVE dep_closure(comp_id, d) AS (
              SELECT id::bigint, 0 FROM nodes WHERE id = ANY(%s)
              UNION
              SELECT e.src_id, dc.d + 1
                FROM dep_closure dc
                JOIN edges e ON e.dst_id = dc.comp_id AND e.rel = 'dependsOn'
               WHERE dc.d < 5
            )
            SELECT DISTINCT n.id, n.ref, n.props_json->>'name', MIN(dc.d)
              FROM dep_closure dc
              JOIN nodes n ON n.id = dc.comp_id
             WHERE n.type = 'Component'
                   """ + ("AND n.project_id = %s" if project_id else "") + """
             GROUP BY n.id, n.ref, n.props_json->>'name'
             ORDER BY MIN(dc.d), n.ref
            """,
            ([target_comp_ids, project_id] if project_id else [target_comp_ids]),
        ).fetchall()
        all_comp = {}
        for cid, cref, cname, dep_d in rows:
            is_target = cid in target_comp_meta
            all_comp[cid] = {
                "id": cid, "ref": cref, "name": cname,
                "severity": "high" if (is_target or dep_d <= 1) else "medium",
                "via": "target" if is_target else "dependsOn",
                "dep_depth": dep_d,
            }

        # At-risk Requirements — OTHER Requirements impacting any Component in closure.
        # Exclude self so the caller sees external regression risk only.
        comp_ids = list(all_comp.keys())
        rows = c.execute(
            """
            SELECT r.id, r.ref, r.props_json->>'title', e.dst_id
              FROM nodes r
              JOIN edges e ON e.src_id = r.id AND e.rel = 'impacts' AND e.dst_id = ANY(%s)
             WHERE r.type = 'Requirement' AND r.id <> %s
                   """ + ("AND r.project_id = %s" if project_id else "") + """
            """,
            ([comp_ids, req_id, project_id] if project_id else [comp_ids, req_id]),
        ).fetchall()
        at_risk_reqs = {}
        for rid, rref, rtitle, comp_id in rows:
            entry = at_risk_reqs.setdefault(rid, {
                "id": rid, "ref": rref, "title": rtitle,
                "impacted_components": [], "severity": "low",
            })
            csev = all_comp[comp_id]["severity"]
            entry["impacted_components"].append(all_comp[comp_id]["ref"])
            if _sev_rank[csev] > _sev_rank[entry["severity"]]:
                entry["severity"] = csev

        # ACs — self Requirement (severity HIGH) + at-risk Requirements (inherit).
        acs_by_id = {}
        req_scope_ids = [req_id] + list(at_risk_reqs.keys())
        sev_by_req = {req_id: "high"}
        ref_by_req = {req_id: req_ref}
        for r in at_risk_reqs.values():
            sev_by_req[r["id"]] = r["severity"]
            ref_by_req[r["id"]] = r["ref"]
        rows = c.execute(
            """
            SELECT a.id, a.ref, a.props_json->>'title', e.src_id
              FROM nodes a
              JOIN edges e ON e.dst_id = a.id AND e.rel = 'has' AND e.src_id = ANY(%s)
             WHERE a.type = 'AcceptanceCriterion'
                   """ + ("AND a.project_id = %s" if project_id else "") + """
            """,
            ([req_scope_ids, project_id] if project_id else [req_scope_ids]),
        ).fetchall()
        self_ac_ids = set()
        for aid, aref, atitle, rid in rows:
            acs_by_id.setdefault(aid, {
                "id": aid, "ref": aref, "title": atitle,
                "severity": sev_by_req[rid],
                "parent_requirement": ref_by_req[rid],
            })
            if rid == req_id:
                self_ac_ids.add(aid)

        # TestCases — coveredBy the collected ACs. Also track which self ACs have
        # zero TestCase coverage for the `self_coverage_gap` bucket.
        tcs_by_id = {}
        covered_self_ac_ids = set()
        if acs_by_id:
            ac_ids = list(acs_by_id.keys())
            sev_by_ac = {a["id"]: a["severity"] for a in acs_by_id.values()}
            ref_by_ac = {a["id"]: a["ref"]      for a in acs_by_id.values()}
            rows = c.execute(
                """
                SELECT t.id, t.ref, t.props_json->>'title', t.props_json->>'priority', e.src_id
                  FROM nodes t
                  JOIN edges e ON e.dst_id = t.id AND e.rel = 'coveredBy' AND e.src_id = ANY(%s)
                 WHERE t.type = 'TestCase'
                       """ + ("AND t.project_id = %s" if project_id else "") + """
                """,
                ([ac_ids, project_id] if project_id else [ac_ids]),
            ).fetchall()
            for tid, tref, ttitle, tprio, ac_id in rows:
                entry = tcs_by_id.setdefault(tid, {
                    "id": tid, "ref": tref, "title": ttitle,
                    "priority": tprio,
                    "severity": sev_by_ac[ac_id],
                    "covers_acs": [],
                })
                entry["covers_acs"].append(ref_by_ac[ac_id])
                asev = sev_by_ac[ac_id]
                if _sev_rank[asev] > _sev_rank[entry["severity"]]:
                    entry["severity"] = asev
                if ac_id in self_ac_ids:
                    covered_self_ac_ids.add(ac_id)

    self_gap = [
        {"ref": v["ref"], "title": v["title"]}
        for k, v in acs_by_id.items()
        if k in self_ac_ids and k not in covered_self_ac_ids
    ]

    def _sev_key(x): return (-_sev_rank[x["severity"]], x.get("ref", ""))
    def _strip(x): return {k: v for k, v in x.items() if k != "id"}

    target_list = sorted(
        [v for v in all_comp.values() if v["via"] == "target"], key=_sev_key)
    at_risk_comp_list = sorted(
        [v for v in all_comp.values() if v["via"] == "dependsOn"], key=_sev_key)
    req_list = sorted(at_risk_reqs.values(), key=_sev_key)
    ac_list  = sorted(acs_by_id.values(), key=_sev_key)
    tc_list  = sorted(tcs_by_id.values(), key=_sev_key)

    return {
        "requirement":            {"ref": req_ref, "title": req_title},
        "target_components":      [_strip(v) for v in target_list],
        "at_risk_components":     [_strip(v) for v in at_risk_comp_list],
        "at_risk_requirements":   [_strip(r) for r in req_list],
        "affected_acs":           [_strip(a) for a in ac_list],
        "affected_testcases":     [_strip(t) for t in tc_list],
        "self_coverage_gap":      self_gap,
        "severity_counts": {
            "components":   {s: sum(1 for v in all_comp.values() if v["severity"] == s) for s in ("high","medium","low")},
            "requirements": {s: sum(1 for r in req_list if r["severity"] == s) for s in ("high","medium","low")},
            "acs":          {s: sum(1 for a in ac_list  if a["severity"] == s) for s in ("high","medium","low")},
            "testcases":    {s: sum(1 for t in tc_list  if t["severity"] == s) for s in ("high","medium","low")},
        },
    }


# ---------------------------------------------------------------------------
# code_impact — impact analysis for a code change (diff files / CodeUnit refs)
# ---------------------------------------------------------------------------
# Ontology walked:
#   CodeUnit <-imports/calls/references- CodeUnit  (code closure)
#   Component -implementedBy-> CodeUnit             (feature ownership)
#   Component -dependsOn-> Component                (business dependency)
#   Requirement -impacts-> Component                (feature scope)
#   Requirement -has-> AcceptanceCriterion          (contract items)
#
# Given a diff (list of source files touched), returns which Requirements/ACs
# might be affected — via the union of code-closure impact and Component
# dependsOn closure. This is what a QE agent uses to answer "what should I
# re-test?" for a MR.
_CODE_EDGE_RELS = (
    'imports', 'imports_from', 'calls', 'references', 're_exports', 'indirect_call',
    # BE (Python) additions from graphify:
    'uses',      # class/instance uses another class
    'inherits',  # subclass relationship — change to parent affects children
)



def code_impact(target, direction='downstream', depth=3, project_id=None):
    """Impact analysis for a code change.

    Args:
      target:    a file path (str), a CodeUnit ref (str), or a list of either.
                 File paths are matched against CodeUnit.props_json->>'source_file'.
      direction: 'downstream' (default) = "who uses THIS" — for MR impact.
                 'upstream'              = "what does THIS use" — for dep audit.
      depth:     max recursion depth over code edges. Default 3 (empirically
                 the sweet spot: catches page → hook → feature helper chains
                 without exploding to the whole app).
      project_id: filter to a single project. Recommended.

    Returns dict:
      {
        "seed_files":            [source_file, ...],
        "seed_code_units":       [ref, ...],
        "affected_code_units":   [{ref, file, label}, ...],
        "affected_components":   [{ref, name, via}, ...],   # via: 'direct' | 'dependsOn'
        "affected_requirements": [{ref, title}, ...],
        "affected_acs":          [{ref, title}, ...],
        "depth_used":            int,
      }
    """
    if isinstance(target, str):
        target = [target]
    if not target:
        return {"seed_files": [], "seed_code_units": [], "affected_code_units": [],
                "affected_components": [], "affected_requirements": [],
                "affected_acs": [], "depth_used": 0}
    if direction not in ('downstream', 'upstream'):
        raise ValueError(f"direction must be 'downstream' or 'upstream', got {direction!r}")

    with conn() as c:
        # 1) Resolve target → seed CodeUnit ids
        #    Support two forms per target: a file path (contains '/') or a CodeUnit ref.
        seed_ids = []
        seed_files = []
        # CodeUnit ref format is "<project_id>:<graph_node_id>" — no '/' allowed.
        # Source file paths always contain '/'. Use '/' as the discriminator so
        # detection works even when project_id is None (agent called without
        # channel-project binding).
        for t in target:
            is_path = '/' in t
            if is_path:
                # Treat as source_file path
                sql = "SELECT id FROM nodes WHERE type='CodeUnit' AND props_json->>'source_file' = %s"
                params = [t]
                if project_id is not None:
                    sql += " AND project_id=%s"; params.append(project_id)
                rows = c.execute(sql, params).fetchall()
                if rows:
                    seed_files.append(t)
                    seed_ids.extend(r[0] for r in rows)
            else:
                # Treat as CodeUnit ref
                sql = "SELECT id, props_json->>'source_file' FROM nodes WHERE type='CodeUnit' AND ref=%s"
                params = [t]
                if project_id is not None:
                    sql += " AND project_id=%s"; params.append(project_id)
                row = c.execute(sql, params).fetchone()
                if row:
                    seed_ids.append(row[0])
                    if row[1]:
                        seed_files.append(row[1])

        seed_ids = list(dict.fromkeys(seed_ids))   # dedupe preserving order
        seed_files = list(dict.fromkeys(seed_files))
        if not seed_ids:
            return {"seed_files": [], "seed_code_units": list(target),
                    "affected_code_units": [], "affected_components": [],
                    "affected_requirements": [], "affected_acs": [],
                    "depth_used": 0, "warning": "No CodeUnit matched target"}

        # 2) Recursive walk over code edges
        # downstream = follow INCOMING edges (dst=seed → src is a consumer)
        # upstream   = follow OUTGOING edges (src=seed → dst is a dependency)
        if direction == 'downstream':
            step_sql = "e.src_id AS next_id, cc.d + 1 AS d FROM code_closure cc JOIN edges e ON e.dst_id = cc.unit_id"
        else:
            step_sql = "e.dst_id AS next_id, cc.d + 1 AS d FROM code_closure cc JOIN edges e ON e.src_id = cc.unit_id"

        code_edge_rels_tuple = _CODE_EDGE_RELS
        rows = c.execute(
            f"""
            WITH RECURSIVE code_closure(unit_id, d) AS (
              SELECT id, 0 FROM nodes WHERE id = ANY(%s)
              UNION
              SELECT {step_sql}
              WHERE e.rel = ANY(%s) AND cc.d < %s
            )
            SELECT DISTINCT cc.unit_id, n.ref, n.props_json->>'source_file', n.props_json->>'label', MIN(cc.d)
              FROM code_closure cc
              JOIN nodes n ON n.id = cc.unit_id
             WHERE n.type = 'CodeUnit'
             GROUP BY cc.unit_id, n.ref, n.props_json->>'source_file', n.props_json->>'label'
             ORDER BY MIN(cc.d), n.ref
            """,
            (seed_ids, list(code_edge_rels_tuple), depth),
        ).fetchall()
        affected_units = [{"id": r[0], "ref": r[1], "file": r[2], "label": r[3], "depth": r[4]} for r in rows]
        touched_unit_ids = [r["id"] for r in affected_units]

        # 3) Direct Components (via implementedBy) + min depth of touched files
        #    Severity signal: min_depth == 0 → HIGH (change lives in this Component);
        #                     min_depth >= 1 → MEDIUM (Component reached transitively);
        #                     via='dependsOn' → LOW (no touched CodeUnit, only business dep).
        unit_depth = {u["id"]: u["depth"] for u in affected_units}
        rows = c.execute(
            """
            SELECT c.id, c.ref, c.props_json->>'name', e.dst_id
              FROM nodes c
              JOIN edges e ON e.src_id = c.id AND e.rel = 'implementedBy'
             WHERE c.type = 'Component' AND e.dst_id = ANY(%s)
                   """ + ("AND c.project_id = %s" if project_id else "") + """
            """,
            ([touched_unit_ids, project_id] if project_id else [touched_unit_ids]),
        ).fetchall()
        direct_comp = {}
        for cid, cref, cname, unit_id in rows:
            d = unit_depth.get(unit_id)
            entry = direct_comp.setdefault(cid, {
                "id": cid, "ref": cref, "name": cname, "via": "direct",
                "min_depth": d if d is not None else 999,
                "touched_units": 0,
            })
            entry["touched_units"] += 1
            if d is not None and d < entry["min_depth"]:
                entry["min_depth"] = d

        # 4) Extend via dependsOn closure — Components that depend on affected ones
        all_comp = dict(direct_comp)
        if direct_comp:
            direct_ids = list(direct_comp.keys())
            rows = c.execute(
                """
                WITH RECURSIVE dep_closure(comp_id, d) AS (
                  SELECT id, 0 FROM nodes WHERE id = ANY(%s)
                  UNION
                  SELECT e.src_id, dc.d + 1
                    FROM dep_closure dc
                    JOIN edges e ON e.dst_id = dc.comp_id AND e.rel = 'dependsOn'
                   WHERE dc.d < 5
                )
                SELECT DISTINCT n.id, n.ref, n.props_json->>'name'
                  FROM dep_closure dc
                  JOIN nodes n ON n.id = dc.comp_id
                 WHERE n.type = 'Component'
                       """ + ("AND n.project_id = %s" if project_id else "") + """
                """,
                ([direct_ids, project_id] if project_id else [direct_ids]),
            ).fetchall()
            for cid, cref, cname in rows:
                if cid not in all_comp:
                    all_comp[cid] = {"id": cid, "ref": cref, "name": cname,
                                     "via": "dependsOn", "min_depth": None,
                                     "touched_units": 0}

        # Assign severity to each Component
        def _severity(entry):
            if entry["via"] == "dependsOn":
                return "low"
            if entry["min_depth"] == 0:
                return "high"
            return "medium"

        for v in all_comp.values():
            v["severity"] = _severity(v)

        # 5) Requirements: impacts INCOMING to affected Components. Also collect
        #    which Components each Requirement impacts, so we can inherit the
        #    MAX severity across those Components.
        affected_reqs = []
        affected_acs  = []
        _sev_rank = {"high": 3, "medium": 2, "low": 1}

        if all_comp:
            comp_ids = list(all_comp.keys())
            rows = c.execute(
                """
                SELECT r.id, r.ref, r.props_json->>'title', e.dst_id
                  FROM nodes r
                  JOIN edges e ON e.src_id = r.id AND e.rel = 'impacts' AND e.dst_id = ANY(%s)
                 WHERE r.type = 'Requirement'
                       """ + ("AND r.project_id = %s" if project_id else "") + """
                """,
                ([comp_ids, project_id] if project_id else [comp_ids]),
            ).fetchall()
            reqs_by_id = {}
            for rid, rref, rtitle, comp_id in rows:
                entry = reqs_by_id.setdefault(rid, {
                    "id": rid, "ref": rref, "title": rtitle,
                    "impacted_components": [],
                    "severity": "low",
                })
                comp_ref = all_comp[comp_id]["ref"]
                comp_sev = all_comp[comp_id]["severity"]
                entry["impacted_components"].append(comp_ref)
                if _sev_rank[comp_sev] > _sev_rank[entry["severity"]]:
                    entry["severity"] = comp_sev
            affected_reqs = list(reqs_by_id.values())

            # 6) ACs: has OUTGOING from those Requirements — inherit Req severity
            if affected_reqs:
                req_ids = [r["id"] for r in affected_reqs]
                sev_by_req_id = {r["id"]: r["severity"] for r in affected_reqs}
                rows = c.execute(
                    """
                    SELECT a.id, a.ref, a.props_json->>'title', e.src_id
                      FROM nodes a
                      JOIN edges e ON e.dst_id = a.id AND e.rel = 'has' AND e.src_id = ANY(%s)
                     WHERE a.type = 'AcceptanceCriterion'
                           """ + ("AND a.project_id = %s" if project_id else "") + """
                    """,
                    ([req_ids, project_id] if project_id else [req_ids]),
                ).fetchall()
                acs_by_id = {}
                for aid, aref, atitle, req_id in rows:
                    parent_sev = sev_by_req_id.get(req_id, "low")
                    entry = acs_by_id.setdefault(aid, {
                        "id": aid, "ref": aref, "title": atitle,
                        "severity": parent_sev,
                        "parent_requirement": next(r["ref"] for r in affected_reqs if r["id"] == req_id),
                    })
                    if _sev_rank[parent_sev] > _sev_rank[entry["severity"]]:
                        entry["severity"] = parent_sev
                affected_acs = list(acs_by_id.values())

        # 7) TestCases: coveredBy OUTGOING from those ACs. TestCase inherits
        #    severity from its AC (which inherited from its parent Requirement).
        affected_testcases = []
        if affected_acs:
            ac_ids = [a["id"] for a in affected_acs]
            sev_by_ac_id = {a["id"]: a["severity"] for a in affected_acs}
            ref_by_ac_id = {a["id"]: a["ref"] for a in affected_acs}
            rows = c.execute(
                """
                SELECT t.id, t.ref, t.props_json->>'title', t.props_json->>'priority', e.src_id
                  FROM nodes t
                  JOIN edges e ON e.dst_id = t.id AND e.rel = 'coveredBy' AND e.src_id = ANY(%s)
                 WHERE t.type = 'TestCase'
                       """ + ("AND t.project_id = %s" if project_id else "") + """
                """,
                ([ac_ids, project_id] if project_id else [ac_ids]),
            ).fetchall()
            tcs_by_id = {}
            for tid, tref, ttitle, tprio, ac_id in rows:
                inherited_sev = sev_by_ac_id.get(ac_id, "low")
                entry = tcs_by_id.setdefault(tid, {
                    "id": tid, "ref": tref, "title": ttitle,
                    "priority": tprio,           # TC's own priority (P1..P4 from Excel), if any
                    "severity": inherited_sev,   # inherited from AC → Requirement chain
                    "covers_acs": [],
                })
                entry["covers_acs"].append(ref_by_ac_id[ac_id])
                if _sev_rank[inherited_sev] > _sev_rank[entry["severity"]]:
                    entry["severity"] = inherited_sev
            affected_testcases = list(tcs_by_id.values())

    # Sort by severity (high → medium → low), then by ref for stability
    def _sev_key(x): return (-_sev_rank[x["severity"]], x.get("ref", ""))
    comp_list = sorted(all_comp.values(), key=_sev_key)
    req_list  = sorted(affected_reqs, key=_sev_key)
    ac_list   = sorted(affected_acs,  key=_sev_key)
    tc_list   = sorted(affected_testcases, key=_sev_key)

    # Strip internal ids from returned dicts (LLM/user doesn't need them)
    def _strip(x): return {k: v for k, v in x.items() if k != "id"}
    return {
        "seed_files":            seed_files,
        "seed_code_units":       [u["ref"] for u in affected_units if u["depth"] == 0],
        "affected_code_units":   [_strip(u) for u in affected_units],
        "affected_components":   [_strip(v) for v in comp_list],
        "affected_requirements": [_strip(r) for r in req_list],
        "affected_acs":          [_strip(a) for a in ac_list],
        "affected_testcases":    [_strip(t) for t in tc_list],
        "severity_counts": {
            "components":   {s: sum(1 for v in comp_list if v["severity"] == s) for s in ("high","medium","low")},
            "requirements": {s: sum(1 for r in req_list  if r["severity"] == s) for s in ("high","medium","low")},
            "acs":          {s: sum(1 for a in ac_list   if a["severity"] == s) for s in ("high","medium","low")},
            "testcases":    {s: sum(1 for t in tc_list   if t["severity"] == s) for s in ("high","medium","low")},
        },
        "depth_used":            depth,
        "direction":             direction,
    }
def get_node_props(ref, type_=None):
    """Return props_json (dict) for the node with this ref (optionally by type), or {}."""
    sql = "SELECT props_json FROM nodes WHERE ref=%s"
    params = [ref]
    if type_ is not None:
        sql += " AND type=%s"
        params.append(type_)
    sql += " ORDER BY id LIMIT 1"
    with conn() as c:
        row = c.execute(sql, params).fetchone()
    return (row[0] if row and row[0] else {}) or {}


def node_id_for(ref, type_=None):
    """Return the id of the node with this ref (optionally filtered by type), or None."""
    sql = "SELECT id FROM nodes WHERE ref=%s"
    params = [ref]
    if type_ is not None:
        sql += " AND type=%s"
        params.append(type_)
    sql += " ORDER BY id LIMIT 1"
    with conn() as c:
        row = c.execute(sql, params).fetchone()
        return row[0] if row else None


def ensure_edge(src_id, rel, dst_id, props=None):
    """Insert an edge only if an identical (src, rel, dst) edge doesn't already exist.
    Returns the edge id. Idempotent."""
    with conn() as c:
        row = c.execute(
            "SELECT id FROM edges WHERE src_id=%s AND rel=%s AND dst_id=%s",
            (src_id, rel, dst_id),
        ).fetchone()
        if row:
            return row[0]
        row = c.execute(
            "INSERT INTO edges(src_id, rel, dst_id, props_json) VALUES (%s,%s,%s,%s) RETURNING id",
            (src_id, rel, dst_id, psycopg.types.json.Json(props or {})),
        ).fetchone()
        return row[0]


def delete_edges_by_dst(src_id, rel, dst_ids):
    """Delete edges (src_id, rel, dst_id) for every dst_id in dst_ids.

    Mirror of ensure_edge for the "the set of dsts a src links to shrunk"
    case (e.g. a Requirement's Confluence links were narrowed). Returns
    number of rows deleted."""
    if not dst_ids:
        return 0
    with conn() as c:
        return c.execute(
            "DELETE FROM edges WHERE src_id=%s AND rel=%s AND dst_id = ANY(%s)",
            (src_id, rel, list(dst_ids)),
        ).rowcount


def update_node_props(ref, key, value, type_=None):
    """Set props_json[key] = value on the node(s) with this ref (optionally by type).
    Leaves other props intact. Returns number of rows updated."""
    sql = (
        "UPDATE nodes SET props_json = jsonb_set(COALESCE(props_json,'{}'::jsonb), "
        "%s::text[], %s::jsonb, true) WHERE ref=%s"
    )
    params = [[key], psycopg.types.json.Json(value), ref]
    if type_ is not None:
        sql += " AND type=%s"
        params.append(type_)
    with conn() as c:
        return c.execute(sql, params).rowcount


def delete_node_by_ref(ref, type_=None):
    """Delete a node (and all edges touching it) by ref. Idempotent — returns True if
    a node was removed, False if none existed."""
    nid = node_id_for(ref, type_)
    if nid is None:
        return False
    with conn() as c:
        c.execute("DELETE FROM edges WHERE src_id=%s OR dst_id=%s", (nid, nid))
        c.execute("DELETE FROM nodes WHERE id=%s", (nid,))
    return True


# --- Layer C: KB promotion / curator flow -----------------------------------

def add_candidate_rule(rule, scope, applies_to, evidence=None):
    """Insert a candidate rule into promotion_queue (status 'pending'). Returns its id."""
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO promotion_queue (candidate_rule, scope, applies_to, evidence, status)
            VALUES (%s, %s, %s, %s, 'pending')
            RETURNING id
            """,
            (rule, scope, applies_to, psycopg.types.json.Json(evidence or {})),
        ).fetchone()
        return row[0]


def get_candidate(candidate_id):
    """Return a promotion_queue row as a dict, or None if it doesn't exist."""
    with conn() as c:
        cur = c.execute(
            """
            SELECT id, candidate_rule, scope, applies_to, evidence, status
            FROM promotion_queue WHERE id=%s
            """,
            (candidate_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip([d.name for d in cur.description], row))


def update_candidate_rule(candidate_id, new_text):
    """Update a candidate's rule text (used by the curator 'Edit' flow)."""
    with conn() as c:
        c.execute(
            "UPDATE promotion_queue SET candidate_rule=%s WHERE id=%s",
            (new_text, candidate_id),
        )


def _index_rule_to_chroma(kb_rule_id, rule, scope, applies_to, approver):
    """Best-effort: index an approved rule into Chroma so search_kb can retrieve it.

    Idempotent — upsert keyed by 'kbrule-<id>', so re-approving/backfilling the same
    rule overwrites rather than duplicating. A Chroma failure must NOT undo the DB
    write; kb_rules stays the system-of-record and backfill_kb_rules_to_chroma() can
    repair the index later. Chroma metadata values must be scalars (no None).
    """
    try:
        from . import rag
        rag.index_docs([(
            f"kbrule-{kb_rule_id}",
            rule or "",
            {
                "type": "rule",
                "source": "curator",
                "applies_to": applies_to or "",
                "scope": scope or "",
                "kb_rule_id": kb_rule_id,
                "approved_by": approver or "",
            },
        )])
        return True
    except Exception:
        return False


def approve_candidate(candidate_id, approver):
    """Promote a pending candidate into kb_rules (status 'active') with provenance,
    then mark the queue row 'approved'. Also index the rule into Chroma so search_kb
    can retrieve it. Returns the new kb_rules id, or None if the candidate does not exist."""
    with conn() as c:
        cand = c.execute(
            "SELECT candidate_rule, scope, applies_to, evidence FROM promotion_queue WHERE id=%s",
            (candidate_id,),
        ).fetchone()
        if not cand:
            return None
        rule, scope, applies_to, evidence = cand
        provenance = {
            "source_candidate": candidate_id,
            "approved_by": approver,
            "evidence": evidence or {},
        }
        row = c.execute(
            """
            INSERT INTO kb_rules (rule, scope, applies_to, status, provenance)
            VALUES (%s, %s, %s, 'active', %s)
            RETURNING id
            """,
            (rule, scope, applies_to, psycopg.types.json.Json(provenance)),
        ).fetchone()
        c.execute(
            "UPDATE promotion_queue SET status='approved' WHERE id=%s",
            (candidate_id,),
        )
        kb_rule_id = row[0]

    # DB is committed at this point; also make the rule retrievable via Chroma.
    _index_rule_to_chroma(kb_rule_id, rule, scope, applies_to, approver)
    return kb_rule_id


def backfill_kb_rules_to_chroma():
    """Index all active kb_rules rows into Chroma (idempotent). Use once to make
    already-approved rules retrievable. Returns the number of rules indexed."""
    with conn() as c:
        rows = c.execute(
            "SELECT id, rule, scope, applies_to, provenance FROM kb_rules "
            "WHERE status='active' ORDER BY id"
        ).fetchall()
    count = 0
    for kb_id, rule, scope, applies_to, provenance in rows:
        approver = provenance.get("approved_by", "") if isinstance(provenance, dict) else ""
        if _index_rule_to_chroma(kb_id, rule, scope, applies_to, approver):
            count += 1
    return count


def reject_candidate(candidate_id, approver):
    """Mark a candidate 'rejected'. Does NOT write kb_rules. Returns True if a row changed."""
    with conn() as c:
        cur = c.execute(
            "UPDATE promotion_queue SET status='rejected' WHERE id=%s",
            (candidate_id,),
        )
        return cur.rowcount > 0


def record_golive_decision(requirement_ref, decision, approver, reason=None):
    """Record a human go-live sign-off (decision 'approved'/'rejected'). Returns the new id."""
    provenance = {
        "requirement": requirement_ref,
        "decision": decision,
        "approved_by": approver,
    }
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO go_live_decisions (requirement_ref, decision, approved_by, reason, provenance)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (requirement_ref, decision, approver, reason, psycopg.types.json.Json(provenance)),
        ).fetchone()
        return row[0]


# Bug severities that block a go-live decision.
HIGH_SEVERITIES = ("critical", "high")


def _uncovered_acs(c, requirement_ref=None, project_id=None):
    # AcceptanceCriterion not coveredBy any TestCase → coverage gap.
    # Ontology: Requirement -has-> AcceptanceCriterion -coveredBy-> TestCase,
    # so an AC is the src of its 'coveredBy' edge.
    # - requirement_ref: scope to that requirement's ACs.
    # - project_id: restrict to ACs belonging to that project (prevents cross-tenant leak).
    sql = """
    SELECT ac.id, ac.ref FROM nodes ac
    WHERE ac.type='AcceptanceCriterion'
      AND NOT EXISTS (
        SELECT 1 FROM edges cov
        WHERE cov.src_id=ac.id AND cov.rel='coveredBy'
      )
    """
    params = []
    if project_id is not None:
        sql += " AND ac.project_id=%s"
        params.append(project_id)
    if requirement_ref is not None:
        sql += """
      AND EXISTS (
        SELECT 1 FROM edges h
        JOIN nodes r ON r.id=h.src_id AND r.type='Requirement'
        WHERE h.dst_id=ac.id AND h.rel='has' AND r.ref=%s
      )
        """
        params.append(requirement_ref)
    return c.execute(sql, params).fetchall()


def coverage_gap(project_id=None):
    """List AC nodes without any coveredBy edge.

    Args:
      project_id: if given, restrict to ACs belonging to that project (multi-tenant
                  isolation). None (default) = global.
    """
    with conn() as c:
        return _uncovered_acs(c, project_id=project_id)


def trace(requirement_ref, project_id=None):
    """Walk Requirement -> AC -> TestCase -> TestRun -> Bug for one requirement.

    Args:
      requirement_ref: external ref of the requirement to trace.
      project_id: if given, requirement must belong to that project (prevents
                  cross-tenant lookups). None (default) = any project.
    """
    with conn() as c:
        sql = "SELECT id, ref FROM nodes WHERE type='Requirement' AND ref=%s"
        params = [requirement_ref]
        if project_id is not None:
            sql += " AND project_id=%s"
            params.append(project_id)
        req = c.execute(sql, params).fetchone()
        if not req:
            return {"requirement": requirement_ref, "found": False, "acceptance_criteria": []}
        req_id = req[0]

        acs = c.execute(
            """
            SELECT ac.id, ac.ref FROM nodes ac
            JOIN edges h ON h.dst_id=ac.id AND h.rel='has'
            WHERE h.src_id=%s AND ac.type='AcceptanceCriterion'
            ORDER BY ac.ref
            """,
            (req_id,),
        ).fetchall()

        acceptance_criteria = []
        for ac_id, ac_ref in acs:
            testcases = c.execute(
                """
                SELECT tc.id, tc.ref FROM nodes tc
                JOIN edges cov ON cov.dst_id=tc.id AND cov.rel='coveredBy'
                WHERE cov.src_id=%s AND tc.type='TestCase'
                ORDER BY tc.ref
                """,
                (ac_id,),
            ).fetchall()

            tc_list = []
            for tc_id, tc_ref in testcases:
                runs = c.execute(
                    """
                    SELECT tr.id, tr.ref, tr.props_json->>'status' FROM nodes tr
                    JOIN edges ex ON ex.dst_id=tr.id AND ex.rel='executedBy'
                    WHERE ex.src_id=%s AND tr.type='TestRun'
                    ORDER BY tr.ref
                    """,
                    (tc_id,),
                ).fetchall()

                run_list = []
                for tr_id, tr_ref, tr_status in runs:
                    bugs = c.execute(
                        """
                        SELECT b.ref, b.props_json->>'status', b.props_json->>'severity'
                        FROM nodes b
                        JOIN edges f ON f.dst_id=b.id AND f.rel='finds'
                        WHERE f.src_id=%s AND b.type='Bug'
                        ORDER BY b.ref
                        """,
                        (tr_id,),
                    ).fetchall()
                    run_list.append({
                        "ref": tr_ref,
                        "status": tr_status,
                        "bugs": [
                            {"ref": br, "status": bs, "severity": bsev}
                            for br, bs, bsev in bugs
                        ],
                    })
                tc_list.append({"ref": tc_ref, "runs": run_list})

            statuses = [r["status"] for tc in tc_list for r in tc["runs"]]
            if not statuses:
                test_status = None
            elif any(s == "fail" for s in statuses):
                test_status = "fail"
            elif all(s == "pass" for s in statuses):
                test_status = "pass"
            else:
                test_status = "mixed"

            acceptance_criteria.append({
                "ref": ac_ref,
                "covered": bool(tc_list),
                "test_status": test_status,
                "testcases": tc_list,
            })

        return {
            "requirement": requirement_ref,
            "found": True,
            "acceptance_criteria": acceptance_criteria,
        }


def failing_tests_for(requirement_ref, project_id=None):
    # TestRuns with status='fail' reachable from the requirement.
    # project_id guards the entry Requirement — prevents cross-tenant lookup.
    sql = """
    SELECT DISTINCT tr.ref, tc.ref
    FROM nodes r
    JOIN edges h   ON h.src_id=r.id  AND h.rel='has'
    JOIN nodes ac  ON ac.id=h.dst_id AND ac.type='AcceptanceCriterion'
    JOIN edges cov ON cov.src_id=ac.id AND cov.rel='coveredBy'
    JOIN nodes tc  ON tc.id=cov.dst_id AND tc.type='TestCase'
    JOIN edges ex  ON ex.src_id=tc.id  AND ex.rel='executedBy'
    JOIN nodes tr  ON tr.id=ex.dst_id  AND tr.type='TestRun'
    WHERE r.type='Requirement' AND r.ref=%s
      AND tr.props_json->>'status'='fail'
    """
    params = [requirement_ref]
    if project_id is not None:
        sql += " AND r.project_id=%s"
        params.append(project_id)
    sql += " ORDER BY tr.ref"
    with conn() as c:
        return c.execute(sql, params).fetchall()


def open_bugs_for(requirement_ref, project_id=None):
    # Open bugs linked to the requirement, either via the test chain
    # (...->TestRun->finds->Bug) or directly via Bug->violates->AC.
    # project_id guards the entry Requirement.
    sql = """
    SELECT DISTINCT b.ref, b.props_json->>'severity'
    FROM nodes b
    WHERE b.type='Bug' AND b.props_json->>'status'='open'
      AND (
        EXISTS (
          SELECT 1
          FROM edges f
          JOIN nodes tr  ON tr.id=f.src_id   AND tr.type='TestRun'
          JOIN edges ex  ON ex.dst_id=tr.id  AND ex.rel='executedBy'
          JOIN nodes tc  ON tc.id=ex.src_id  AND tc.type='TestCase'
          JOIN edges cov ON cov.dst_id=tc.id AND cov.rel='coveredBy'
          JOIN nodes ac  ON ac.id=cov.src_id AND ac.type='AcceptanceCriterion'
          JOIN edges h   ON h.dst_id=ac.id   AND h.rel='has'
          JOIN nodes r   ON r.id=h.src_id    AND r.type='Requirement'
          WHERE f.dst_id=b.id AND f.rel='finds' AND r.ref=%s
    """
    params = [requirement_ref]
    if project_id is not None:
        sql += " AND r.project_id=%s"
        params.append(project_id)
    sql += """
        )
        OR EXISTS (
          SELECT 1
          FROM edges v
          JOIN nodes ac ON ac.id=v.dst_id AND ac.type='AcceptanceCriterion'
          JOIN edges h  ON h.dst_id=ac.id AND h.rel='has'
          JOIN nodes r  ON r.id=h.src_id  AND r.type='Requirement'
          WHERE v.src_id=b.id AND v.rel='violates' AND r.ref=%s
    """
    params.append(requirement_ref)
    if project_id is not None:
        sql += " AND r.project_id=%s"
        params.append(project_id)
    sql += """
        )
      )
    ORDER BY b.ref
    """
    with conn() as c:
        return c.execute(sql, params).fetchall()


def bug_blast_radius(bug_ref, project_id=None):
    """How many Requirements/ACs depend on the Component(s) this bug affects.

    Ontology: Bug -affects-> Component <-impacts- Requirement -has-> AcceptanceCriterion.
    Larger blast radius -> higher priority.

    Args:
      bug_ref: external ref of the Bug to analyze.
      project_id: guards the entry Bug (multi-tenant isolation). The blast counts
                  Requirements/ACs across ALL projects — cross-project impact is
                  the point of the metric.
    """
    # `entry_pred` isolates the entry Bug by project (if requested) without
    # restricting the downstream impact graph, so cross-project blast is still counted.
    entry_pred = "b.type='Bug' AND b.ref=%s"
    params = [bug_ref]
    if project_id is not None:
        entry_pred += " AND b.project_id=%s"
        params.append(project_id)
    sql = f"""
    WITH affected AS (
        SELECT DISTINCT comp.id AS comp_id, comp.ref AS comp_ref
        FROM nodes b
        JOIN edges af   ON af.src_id=b.id AND af.rel='affects'
        JOIN nodes comp ON comp.id=af.dst_id AND comp.type='Component'
        WHERE {entry_pred}
    )
    SELECT
      (SELECT array_agg(DISTINCT comp_ref) FROM affected) AS components,
      (SELECT count(DISTINCT r.id)
         FROM affected a
         JOIN edges im ON im.dst_id=a.comp_id AND im.rel='impacts'
         JOIN nodes r  ON r.id=im.src_id AND r.type='Requirement') AS n_requirements,
      (SELECT count(DISTINCT ac.id)
         FROM affected a
         JOIN edges im ON im.dst_id=a.comp_id AND im.rel='impacts'
         JOIN nodes r  ON r.id=im.src_id AND r.type='Requirement'
         JOIN edges h  ON h.src_id=r.id AND h.rel='has'
         JOIN nodes ac ON ac.id=h.dst_id AND ac.type='AcceptanceCriterion') AS n_acs
    """
    with conn() as c:
        row = c.execute(sql, params).fetchone()

    components = (row[0] if row else None) or []
    n_requirements = (row[1] if row else 0) or 0
    n_acs = (row[2] if row else 0) or 0

    # Simple priority heuristic derived from the blast radius.
    score = n_requirements + n_acs
    if score >= 5:
        priority = "P1"
    elif score >= 2:
        priority = "P2"
    elif score >= 1:
        priority = "P3"
    else:
        priority = "P4"

    return {
        "bug": bug_ref,
        "affected_components": components,
        "requirements_impacted": n_requirements,
        "acs_impacted": n_acs,
        "priority": priority,
    }


def go_no_go(requirement_ref, project_id=None):
    """Combine coverage gaps, failing tests and open bugs into a GO/NO-GO call.

    Args:
      requirement_ref: external ref of the Requirement.
      project_id: if given, all sub-queries scope to that project (multi-tenant).
                  Returns decision='NOT_FOUND' if the requirement doesn't exist
                  in that project.
    """
    with conn() as c:
        exists_sql = "SELECT id FROM nodes WHERE type='Requirement' AND ref=%s"
        exists_params = [requirement_ref]
        if project_id is not None:
            exists_sql += " AND project_id=%s"
            exists_params.append(project_id)
        if not c.execute(exists_sql, exists_params).fetchone():
            scope = f" in project '{project_id}'" if project_id else ""
            return {
                "requirement": requirement_ref,
                "decision": "NOT_FOUND",
                "coverage_gaps": [],
                "failing_tests": [],
                "open_bugs": [],
                "next_actions": [f"Requirement '{requirement_ref}' not found{scope}"],
            }

        gaps = _uncovered_acs(c, requirement_ref, project_id=project_id)
    failing = failing_tests_for(requirement_ref, project_id=project_id)
    bugs = open_bugs_for(requirement_ref, project_id=project_id)

    coverage_gaps = [ac_ref for _id, ac_ref in gaps]
    failing_tests = [{"testrun": tr_ref, "testcase": tc_ref} for tr_ref, tc_ref in failing]
    open_bugs = [{"bug": b_ref, "severity": sev} for b_ref, sev in bugs]

    has_high_bug = any((sev or "").lower() in HIGH_SEVERITIES for _b, sev in bugs)
    decision = "GO" if (not coverage_gaps and not failing_tests and not has_high_bug) else "NO-GO"

    next_actions = []
    for ac_ref in coverage_gaps:
        next_actions.append(f"Write a testcase for {ac_ref}")
    for ft in failing_tests:
        next_actions.append(f"Fix failing testcase {ft['testcase']} (run {ft['testrun']})")
    for ob in open_bugs:
        next_actions.append(f"Close bug {ob['bug']} ({ob['severity']})")

    return {
        "requirement": requirement_ref,
        "decision": decision,
        "coverage_gaps": coverage_gaps,
        "failing_tests": failing_tests,
        "open_bugs": open_bugs,
        "next_actions": next_actions,
    }


# --- bug classification (improvement loop) --------------------------------

def classify_bug(bug_ref, project_id=None):
    """Classify how a bug was detected, to route it into the improvement loop.

    Categories (pure graph structure, no heuristics on props_json):
      caught_by_test         Bug has an incoming `finds` edge from a TestRun.
                             OK — QE process worked; no improvement action.
      leaked_impact_missed   Bug `affects` a Component that the parent Requirement
                             did NOT declare `impacts` on. Impact analysis failed
                             to identify this component as at-risk. → improve
                             impact analysis pipeline (component identification).
      leaked_tc_missing      Bug violates AC(s), but AC has no `coveredBy` edge
                             to any TestCase. → improve `gen_testcase`.
      leaked_tc_not_run      Bug violates AC(s) that ARE covered by TestCases,
                             but none of those TestCases have any TestRun.
                             → improve impact analysis / test prioritisation.
      leaked_tc_ran_missed   Bug violates AC(s) with TestCases that DID run
                             (there's ≥1 TestRun), yet the bug still leaked.
                             → improve execution quality / assertions.
      leaked_no_ac_link      Bug has no `violates` edge to any AC. Can't classify
                             automatically — needs a human to link to an AC first.

    Priority: caught_by_test > leaked_impact_missed > (violates-based cases) >
    leaked_no_ac_link. Impact-missed sits BEFORE violates checks because a bug
    in an unforeseen component is a distinct root cause even if a human later
    added an AC link post-hoc — the analysis was still incomplete.

    Requires bug node's `props.jira_parent_ref` to identify the Requirement
    under test (for impact-missed detection). Falls through to violates-based
    checks if jira_parent_ref is absent.

    Args:
      bug_ref: external ref of the Bug (e.g. 'CDM-287').
      project_id: multi-tenant guard. Bug must belong to this project.

    Returns dict with:
      category, improve, reasoning, violated_acs, related_testcases, related_testruns
    """
    with conn() as c:
        # 1. Bug exists + project match
        sql = "SELECT id FROM nodes WHERE type='Bug' AND ref=%s"
        params = [bug_ref]
        if project_id is not None:
            sql += " AND project_id=%s"
            params.append(project_id)
        row = c.execute(sql, params).fetchone()
        if not row:
            scope = f" in project '{project_id}'" if project_id else ""
            return {
                "category": "not_found",
                "improve": None,
                "reasoning": f"Bug '{bug_ref}' not found{scope}",
                "violated_acs": [],
                "related_testcases": [],
                "related_testruns": [],
            }
        bug_id = row[0]

        # 2. Caught by a TestRun?  (incoming `finds` edge)
        finds_row = c.execute(
            """
            SELECT tr.ref FROM edges f
            JOIN nodes tr ON tr.id = f.src_id AND tr.type='TestRun'
            WHERE f.dst_id=%s AND f.rel='finds'
            LIMIT 1
            """,
            (bug_id,),
        ).fetchone()
        if finds_row:
            return {
                "category": "caught_by_test",
                "improve": None,
                "reasoning": f"Detected by TestRun {finds_row[0]} — process worked.",
                "violated_acs": [],
                "related_testcases": [],
                "related_testruns": [finds_row[0]],
            }

        # 3. Leaked in an unforeseen component?
        # Bug `affects` a Component the parent Requirement did NOT declare `impacts`
        # on. This is a distinct failure of the impact-analysis pipeline (component
        # identification), separate from "we knew this feature but had no TC".
        parent_ref_row = c.execute(
            "SELECT props_json->>'jira_parent_ref' FROM nodes WHERE id=%s",
            (bug_id,),
        ).fetchone()
        parent_ref = parent_ref_row[0] if parent_ref_row else None
        if parent_ref:
            unforeseen = c.execute(
                """
                SELECT comp.ref FROM edges af
                JOIN nodes comp ON comp.id = af.dst_id AND comp.type='Component'
                WHERE af.src_id=%s AND af.rel='affects'
                  AND NOT EXISTS (
                    SELECT 1 FROM edges im
                    JOIN nodes r ON r.id = im.src_id
                                AND r.type='Requirement' AND r.ref=%s
                    WHERE im.dst_id = comp.id AND im.rel='impacts'
                  )
                ORDER BY comp.ref
                """,
                (bug_id, parent_ref),
            ).fetchall()
            if unforeseen:
                unforeseen_refs = [r[0] for r in unforeseen]
                return {
                    "category": "leaked_impact_missed",
                    "improve": "impact_analysis",
                    "reasoning": (
                        f"Bug affects Component(s) {unforeseen_refs} but Requirement "
                        f"'{parent_ref}' didn't declare `impacts` on them. Impact "
                        f"analysis missed identifying these components as at-risk."
                    ),
                    "violated_acs": [],
                    "related_testcases": [],
                    "related_testruns": [],
                    "unforeseen_components": unforeseen_refs,
                }

        # 4. Leaked. Find violated ACs.
        violated = c.execute(
            """
            SELECT ac.id, ac.ref FROM edges v
            JOIN nodes ac ON ac.id = v.dst_id AND ac.type='AcceptanceCriterion'
            WHERE v.src_id=%s AND v.rel='violates'
            ORDER BY ac.ref
            """,
            (bug_id,),
        ).fetchall()
        if not violated:
            return {
                "category": "leaked_no_ac_link",
                "improve": "manual_review",
                "reasoning": "Bug has no `violates` edge to any AC. Link it to an AC "
                             "to enable automatic classification.",
                "violated_acs": [],
                "related_testcases": [],
                "related_testruns": [],
            }

        violated_ac_refs = [ref for _, ref in violated]
        violated_ac_ids = tuple(ac_id for ac_id, _ in violated)

        # 4. TestCases covering those ACs
        tc_rows = c.execute(
            """
            SELECT DISTINCT tc.id, tc.ref FROM edges cov
            JOIN nodes tc ON tc.id = cov.dst_id AND tc.type='TestCase'
            WHERE cov.src_id = ANY(%s) AND cov.rel='coveredBy'
            ORDER BY tc.ref
            """,
            (list(violated_ac_ids),),
        ).fetchall()
        tc_refs = [ref for _, ref in tc_rows]
        tc_ids = [tc_id for tc_id, _ in tc_rows]

        if not tc_refs:
            return {
                "category": "leaked_tc_missing",
                "improve": "gen_testcase",
                "reasoning": (f"AC(s) {violated_ac_refs} have no covering TestCase. "
                              f"Agent gen didn't cover this scenario — write a lesson."),
                "violated_acs": violated_ac_refs,
                "related_testcases": [],
                "related_testruns": [],
            }

        # 5. TestRuns for those TestCases
        run_rows = c.execute(
            """
            SELECT DISTINCT tr.ref, tr.props_json->>'status' AS status
            FROM edges ex
            JOIN nodes tr ON tr.id = ex.dst_id AND tr.type='TestRun'
            WHERE ex.src_id = ANY(%s) AND ex.rel='executedBy'
            ORDER BY tr.ref
            """,
            (tc_ids,),
        ).fetchall()

        if not run_rows:
            return {
                "category": "leaked_tc_not_run",
                "improve": "impact_analysis",
                "reasoning": (f"TestCase(s) {tc_refs} exist for AC(s) {violated_ac_refs} "
                              f"but were never executed. Impact analysis missed prioritising them."),
                "violated_acs": violated_ac_refs,
                "related_testcases": tc_refs,
                "related_testruns": [],
            }

        run_refs = [r for r, _ in run_rows]
        return {
            "category": "leaked_tc_ran_missed",
            "improve": "execution_quality",
            "reasoning": (f"TestCase(s) {tc_refs} ran (runs {run_refs}) yet bug leaked. "
                          f"Execution / assertion quality missed it — review testcase depth."),
            "violated_acs": violated_ac_refs,
            "related_testcases": tc_refs,
            "related_testruns": run_refs,
        }


def requirement_with_acs(ref, project_id=None):
    """Return the Requirement's PRD content plus its AcceptanceCriteria.

    Args:
      ref: Requirement ref (e.g. 'CDM-268').
      project_id: if given, requirement must belong to that project.

    Returns:
      {"ref": ref, "found": True, "title": str|None, "detail": str|None,
       "acs": [{"ref": str, "desc": str}]}
      or {"ref": ref, "found": False} if no such Requirement exists.
    """
    sql = "SELECT id, props_json FROM nodes WHERE type='Requirement' AND ref=%s"
    params = [ref]
    if project_id is not None:
        sql += " AND project_id=%s"
        params.append(project_id)
    with conn() as c:
        row = c.execute(sql, params).fetchone()
        if not row:
            return {"ref": ref, "found": False}
        req_id, props = row
        props = props or {}
        acs = c.execute(
            """
            SELECT ac.ref, ac.props_json->>'desc' FROM nodes ac
            JOIN edges h ON h.dst_id=ac.id AND h.rel='has'
            WHERE h.src_id=%s AND ac.type='AcceptanceCriterion'
            ORDER BY ac.ref
            """,
            (req_id,),
        ).fetchall()
    return {
        "ref": ref,
        "found": True,
        "title": props.get("title"),
        "detail": props.get("detail"),
        "acs": [{"ref": ac_ref, "desc": desc} for ac_ref, desc in acs],
    }


def testcases_for_requirement(ref, project_id=None):
    """Return existing TestCase nodes covering any AC of this Requirement.

    Dedup by TestCase ref (a TC can cover multiple AC of the same requirement).
    Each item includes `ac_refs`: every AC of this requirement that this
    TestCase covers. Returns [] if the requirement doesn't exist or has no
    covered ACs yet.
    """
    sql = "SELECT id FROM nodes WHERE type='Requirement' AND ref=%s"
    params = [ref]
    if project_id is not None:
        sql += " AND project_id=%s"
        params.append(project_id)
    with conn() as c:
        row = c.execute(sql, params).fetchone()
        if not row:
            return []
        req_id = row[0]
        rows = c.execute(
            """
            SELECT tc.ref, tc.props_json, ac.ref FROM nodes tc
            JOIN edges cov ON cov.dst_id=tc.id AND cov.rel='coveredBy'
            JOIN nodes ac ON ac.id=cov.src_id AND ac.type='AcceptanceCriterion'
            JOIN edges h ON h.dst_id=ac.id AND h.rel='has'
            WHERE h.src_id=%s AND tc.type='TestCase'
            ORDER BY tc.ref
            """,
            (req_id,),
        ).fetchall()
    by_ref = {}
    for tc_ref, props, ac_ref in rows:
        props = props or {}
        if tc_ref not in by_ref:
            data_variants = props.get("data_variants") or _raw_rows_to_data_variants(props.get("raw_rows"))
            # Legacy/ingested testcases saved before the `type` field existed
            # have no explicit type; infer it rather than defaulting
            # everything to "Normal".
            inferred_type = props.get("type") or ("DataTable" if is_datatable_testcase(props) else "Normal")
            by_ref[tc_ref] = {
                "ref": tc_ref,
                "title": props.get("title"),
                "type": inferred_type,
                "priority": props.get("priority"),
                "precondition": props.get("precondition"),
                "steps": props.get("steps") or [],
                "data_variants": data_variants,
                "api": props.get("api") or {},
                "ac_refs": [],
            }
        by_ref[tc_ref]["ac_refs"].append(ac_ref)
    return list(by_ref.values())


def project_id_from_ref(ref):
    """Canonical project id = the Jira key prefix before the first '-'
    (e.g. 'CDM-268' -> 'CDM'). Returns the ref unchanged if it has no '-'."""
    return ref.split("-")[0] if ref and "-" in ref else ref


def save_testcases(requirement_ref, testcases, approved_by, project_id=None):
    """Upsert draft-schema testcases (tieukiwi/testcase_gen.py) as verified
    TestCase nodes in the CANONICAL props_json shape (see CLAUDE.md _meta contract
    + docs/Gen-testcase-design.md), and ensure a coveredBy edge from each ac_ref.

    Canonical props_json: title, type (Normal|API|DataTable), priority, precondition,
    steps (ARRAY of {description, expected}), data_variants, api, and
    _meta {extraction_source:'llm:gen_testcase', confidence, review_status:'verified',
    approved_by:<slack id>, source_requirement:<REQ key>}.

    Args:
      requirement_ref: the Requirement these testcases belong to; also sets the
                        canonical project_id (= key prefix before '-') on the nodes.
      testcases: list of draft-schema dicts.
      approved_by: identifier (Slack user id) of the human approver.
      project_id: legacy/ignored for the node key — the canonical project is derived
                  from requirement_ref (project_id_from_ref).

    Returns:
      list of TestCase node ids, in the same order as `testcases`.
    """
    tc_project = project_id_from_ref(requirement_ref)
    node_ids = []
    with conn() as c:
        for tc in testcases:
            props = {
                "title": tc["title"],
                "type": tc.get("type") or ("DataTable" if is_datatable_testcase(tc) else "Normal"),
                "priority": tc["priority"],
                "precondition": tc.get("precondition", ""),
                "steps": tc["steps"],
                "api": tc.get("api") or {},
                "data_variants": tc.get("data_variants") or [],
                "_meta": {
                    "extraction_source": "llm:gen_testcase",
                    "confidence": 0.9,
                    "review_status": "verified",
                    "approved_by": approved_by,
                    "source_requirement": requirement_ref,
                },
            }
            row = c.execute(
                """
                INSERT INTO nodes (type, ref, project_id, props_json)
                VALUES ('TestCase', %s, %s, %s)
                ON CONFLICT (project_id, ref) WHERE ref IS NOT NULL DO UPDATE
                  SET props_json = nodes.props_json || EXCLUDED.props_json
                RETURNING id
                """,
                (tc["ref"], tc_project, psycopg.types.json.Json(props)),
            ).fetchone()
            tc_id = row[0]
            node_ids.append(tc_id)
            for ac_ref in tc.get("ac_refs", []):
                # Resolve the AC in the canonical project first, then any project
                # (so links survive nodes whose project_id predates this rule).
                ac_row = c.execute(
                    "SELECT id FROM nodes WHERE type='AcceptanceCriterion' AND ref=%s AND project_id=%s",
                    (ac_ref, tc_project),
                ).fetchone()
                if not ac_row:
                    ac_row = c.execute(
                        "SELECT id FROM nodes WHERE type='AcceptanceCriterion' AND ref=%s ORDER BY id LIMIT 1",
                        (ac_ref,),
                    ).fetchone()
                if not ac_row:
                    continue
                ac_id = ac_row[0]
                exists = c.execute(
                    "SELECT id FROM edges WHERE src_id=%s AND rel='coveredBy' AND dst_id=%s",
                    (ac_id, tc_id),
                ).fetchone()
                if not exists:
                    c.execute(
                        "INSERT INTO edges(src_id, rel, dst_id, props_json) VALUES (%s,'coveredBy',%s,%s)",
                        (ac_id, tc_id, psycopg.types.json.Json({})),
                    )
    return node_ids