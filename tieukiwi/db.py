import psycopg
from contextlib import contextmanager

from .config import DATABASE_URL

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


def upsert_node_by_ref(type_, ref, props=None):
    # Insert a node, or update its props if one with the same (type, ref) already exists.
    # Avoids duplicates when re-fetching the same external item (e.g. a Jira issue).
    props = props or {}
    with conn() as c:
        row = c.execute(
            "SELECT id FROM nodes WHERE type=%s AND ref=%s ORDER BY id LIMIT 1",
            (type_, ref),
        ).fetchone()
        if row:
            node_id = row[0]
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
            by_ref[tc_ref] = {
                "ref": tc_ref,
                "title": props.get("title"),
                "priority": props.get("priority"),
                "precondition": props.get("precondition"),
                "steps": props.get("steps") or [],
                "data_variants": props.get("data_variants") or [],
                "ac_refs": [],
            }
        by_ref[tc_ref]["ac_refs"].append(ac_ref)
    return list(by_ref.values())