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

# Bug severities that block a go-live decision.
HIGH_SEVERITIES = ("critical", "high")


def _uncovered_acs(c, requirement_ref=None):
    # AcceptanceCriterion not coveredBy any TestCase → coverage gap.
    # Ontology: Requirement -has-> AcceptanceCriterion -coveredBy-> TestCase,
    # so an AC is the src of its 'coveredBy' edge.
    # When requirement_ref is given, scope to that requirement's ACs.
    sql = """
    SELECT ac.id, ac.ref FROM nodes ac
    WHERE ac.type='AcceptanceCriterion'
      AND NOT EXISTS (
        SELECT 1 FROM edges cov
        WHERE cov.src_id=ac.id AND cov.rel='coveredBy'
      )
    """
    params = []
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


def coverage_gap():
    with conn() as c:
        return _uncovered_acs(c)


def trace(requirement_ref):
    # Walk Requirement -> AC -> TestCase -> TestRun -> Bug for one requirement.
    with conn() as c:
        req = c.execute(
            "SELECT id, ref FROM nodes WHERE type='Requirement' AND ref=%s",
            (requirement_ref,),
        ).fetchone()
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


def failing_tests_for(requirement_ref):
    # TestRuns with status='fail' reachable from the requirement.
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
    ORDER BY tr.ref
    """
    with conn() as c:
        return c.execute(sql, (requirement_ref,)).fetchall()


def open_bugs_for(requirement_ref):
    # Open bugs linked to the requirement, either via the test chain
    # (...->TestRun->finds->Bug) or directly via Bug->violates->AC.
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
        )
        OR EXISTS (
          SELECT 1
          FROM edges v
          JOIN nodes ac ON ac.id=v.dst_id AND ac.type='AcceptanceCriterion'
          JOIN edges h  ON h.dst_id=ac.id AND h.rel='has'
          JOIN nodes r  ON r.id=h.src_id  AND r.type='Requirement'
          WHERE v.src_id=b.id AND v.rel='violates' AND r.ref=%s
        )
      )
    ORDER BY b.ref
    """
    with conn() as c:
        return c.execute(sql, (requirement_ref, requirement_ref)).fetchall()


def bug_blast_radius(bug_ref):
    # How many Requirements/ACs depend on the Component(s) this bug affects.
    # Ontology: Bug -affects-> Component <-impacts- Requirement -has-> AcceptanceCriterion.
    # Larger blast radius -> higher priority.
    sql = """
    WITH affected AS (
        SELECT DISTINCT comp.id AS comp_id, comp.ref AS comp_ref
        FROM nodes b
        JOIN edges af   ON af.src_id=b.id AND af.rel='affects'
        JOIN nodes comp ON comp.id=af.dst_id AND comp.type='Component'
        WHERE b.type='Bug' AND b.ref=%s
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
        row = c.execute(sql, (bug_ref,)).fetchone()

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


def go_no_go(requirement_ref):
    # Combine coverage gaps, failing tests and open bugs into a GO/NO-GO call.
    with conn() as c:
        gaps = _uncovered_acs(c, requirement_ref)
    failing = failing_tests_for(requirement_ref)
    bugs = open_bugs_for(requirement_ref)

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