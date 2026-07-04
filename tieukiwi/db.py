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