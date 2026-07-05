"""Ask routing: map an entity type to the owner role responsible for it.

Two-tier API:
  - owner_for(entity_type)            -> role name (string). Legacy, class-level.
  - resolve_owner_slack(node_id)      -> user dict (or None). Instance-level with
                                         3-tier fallback + Feedback hop.

Used to route coverage gaps / go-no-go action items to the right person.
The Slack delivery part (actually posting to the owner) is TODO (Layer B).
"""

from . import db


# Entity type (per ontology) -> owner role responsible for fixing gaps on it.
ENTITY_OWNER = {
    "Sprint":              "product-owner",
    "UserStory":           "product-owner",
    "Requirement":         "product-owner",
    "AcceptanceCriterion": "product-owner",   # decided 2026-07-02: AC owned by PO
    "TestCase":            "qa-lead",         # per docs/ontology.md
    "TestPlan":            "qa-lead",
    "TestRun":             "qa-engineer",
    "Bug":                 "dev-owner",
    "Component":           "tech-lead",
    # Feedback: resolves via edge 'about' -> handled in resolve_owner_slack()
}

DEFAULT_OWNER = "qa-lead"

# Canonical role names in the users table (upper snake case).
ROLE_MAP = {
    "product-owner": "PO",
    "qa-lead":       "QE_LEAD",
    "qa-engineer":   "QE_EXECUTOR",
    "dev-owner":     "DEV",
    "tech-lead":     "TECH_LEAD",
    "ba":            "BA",
}


# Layer C: a candidate rule's applies_to (rule domain) -> approver role label.
# TestCase-ish rules are approved by the QE lead; business/spec rules by the PO.
APPROVER_ROLE = {
    "TestCase":            "QE_lead",
    "TestPlan":            "QE_lead",
    "TestRun":             "QE_lead",
    "Bug":                 "QE_lead",
    "AcceptanceCriterion": "PO",
    "Requirement":         "PO",
    "UserStory":           "PO",
    "business":            "PO",
}

DEFAULT_APPROVER = "QE_lead"


def approver_for(applies_to):
    """Return the approver role label for a candidate rule's applies_to domain.

    Demo scope: returns a role LABEL (e.g. "QE_lead") shown as a hint in the curator
    message. TODO: resolve role label -> real Slack user id (config dict / users table)
    for an actual @mention.
    """
    return APPROVER_ROLE.get(applies_to, DEFAULT_APPROVER)


# --- Role resolution for real @mentions (used with db.resolve_role_slack_id) ------
# Lowercase role tokens; db.resolve_role_slack_id matches them case-insensitively
# against the canonical upper-snake values in users.role.

# Decision type -> approver role.
DECISION_ROLE = {
    "go_live":     "delivery_manager",
    "testcase":    "qe_lead",
    "curator":     "qe_lead",
    "business":    "po",
    "requirement": "po",
}

# Candidate-rule domain (applies_to) -> approver role.
_APPLIES_ROLE = {
    "TestCase":            "qe_lead",
    "TestPlan":            "qe_lead",
    "TestRun":             "qe_lead",
    "Bug":                 "qe_lead",
    "AcceptanceCriterion": "po",
    "Requirement":         "po",
    "UserStory":           "po",
    "business":            "po",
}


def approver_role_for(decision_type):
    """Map a decision type to its approver role token (e.g. 'go_live' -> 'delivery_manager')."""
    return DECISION_ROLE.get(decision_type, "qe_lead")


def curator_role_for(applies_to):
    """Map a candidate rule's applies_to domain to its approver role ('qe_lead' | 'po')."""
    return _APPLIES_ROLE.get(applies_to, "qe_lead")


def owner_for(entity_type):
    """Legacy: return the class-level role name for a given entity type."""
    return ENTITY_OWNER.get(entity_type, DEFAULT_OWNER)


def route_gap(entity_type, ref, note=None):
    """Legacy: build a routing record for a gap/action item.

    Prefer resolve_owner_slack() which returns a real user dict.
    """
    return {
        "entity_type": entity_type,
        "ref": ref,
        "owner": owner_for(entity_type),
        "note": note,
    }


def resolve_owner_slack(node_id):
    """Resolve a node to a concrete Slack user, with 3-tier fallback.

    Order:
      1. Instance override: nodes.props_json.owner_slack_id (if present in users)
      2. Project-scoped: users WHERE role = <canonical> AND project_id = <node.project_id>
      3. Global default: users WHERE role = <canonical> AND project_id IS NULL

    Feedback nodes: hop through edge 'about' and resolve the target entity's owner.

    Returns a dict {id, slack_id, display_name, role, project_id, ...} or None.
    """
    with db.conn() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT id, type, ref, project_id, props_json FROM nodes WHERE id = %s",
            (node_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        _, node_type, ref_id, project_id, props = row

        # Feedback: hop to the entity it is 'about'
        if node_type == "Feedback":
            cur.execute(
                """
                SELECT dst_id FROM edges
                WHERE src_id = %s AND rel = 'about' LIMIT 1
                """,
                (node_id,),
            )
            about = cur.fetchone()
            if about:
                return resolve_owner_slack(about[0])
            return None

        # 1. instance-level override via props_json.owner_slack_id
        slack_id = (props or {}).get("owner_slack_id")
        if slack_id:
            cur.execute(
                "SELECT * FROM users WHERE slack_id = %s", (slack_id,)
            )
            r = cur.fetchone()
            if r:
                return _row_to_dict(cur, r)

        # 2 + 3. role-based lookup (project-scoped first, then global)
        role_label = ENTITY_OWNER.get(node_type, DEFAULT_OWNER)
        role_canonical = ROLE_MAP.get(role_label)
        if not role_canonical:
            return None

        cur.execute(
            """
            SELECT * FROM users
            WHERE role = %s
              AND (project_id = %s OR project_id IS NULL)
            ORDER BY (project_id = %s) DESC NULLS LAST
            LIMIT 1
            """,
            (role_canonical, project_id, project_id),
        )
        r = cur.fetchone()
        if r:
            return _row_to_dict(cur, r)
        return None


def _row_to_dict(cur, row):
    """psycopg returns tuples; zip with cursor.description to make a dict."""
    cols = [d.name for d in cur.description]
    return dict(zip(cols, row))
