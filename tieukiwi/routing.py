"""Ask-routing: decide WHICH role owns a problem / decision / entity.

One role system:
  - Canonical role constants below are the SINGLE source of truth (no string
    literals scattered elsewhere).
  - approver_role_for(decision_type) -> role   (problem / decision -> approver)
  - route_gap(entity_type)           -> role   (ontology entity  -> owner)
  - curator_role_for(applies_to)     -> role   (candidate-rule domain -> approver;
                                                routes through approver_role_for)

This module only decides the ROLE. Turning a role into a real Slack user is the
job of ONE helper elsewhere: db.resolve_role_slack_id / db.mention_for.
"""

# --- Canonical roles: the single source of truth ---
DELIVERY_MANAGER = "delivery_manager"
QE_LEAD = "qe_lead"
PO = "po"
DEV = "dev"

# problem / decision type -> approver role (per sheet-General ask-routing).
_DECISION_ROLE = {
    "go_live":       DELIVERY_MANAGER,
    "testcase":      QE_LEAD,
    "coverage_gap":  QE_LEAD,
    "testcase_rule": QE_LEAD,
    "requirement":   PO,
    "brd":           PO,
    "business_rule": PO,
    "po_confirm":    PO,
    "bug":           DEV,
    "failing_test":  DEV,
}
_DEFAULT_APPROVER = QE_LEAD

# ontology entity type -> owner role (for gap / failing-test / bug routing).
_ENTITY_ROLE = {
    "Sprint":              PO,
    "UserStory":           PO,
    "Requirement":         PO,
    "AcceptanceCriterion": PO,
    "TestCase":            QE_LEAD,
    "TestPlan":            QE_LEAD,
    "TestRun":             QE_LEAD,
    "Bug":                 DEV,
    "Component":           DEV,
}
_DEFAULT_OWNER = QE_LEAD

# candidate-rule domain (applies_to) -> decision type. Kept as domain->decision only,
# so role assignments live in exactly ONE table (_DECISION_ROLE via approver_role_for).
_APPLIES_DECISION = {
    "TestCase":            "testcase",
    "TestPlan":            "testcase",
    "TestRun":             "testcase",
    "Bug":                 "bug",
    "AcceptanceCriterion": "business_rule",
    "Requirement":         "business_rule",
    "UserStory":           "business_rule",
    "business":            "business_rule",
}


def approver_role_for(decision_type):
    """Map a problem/decision type to its approver role (single mapping table)."""
    return _DECISION_ROLE.get(decision_type, _DEFAULT_APPROVER)


def route_gap(entity_type):
    """Map an ontology entity type to the role that owns fixing gaps on it."""
    return _ENTITY_ROLE.get(entity_type, _DEFAULT_OWNER)


def curator_role_for(applies_to):
    """Map a candidate rule's applies_to domain to its approver role.

    Routes through approver_role_for so role assignments are never duplicated:
    domain -> decision type (_APPLIES_DECISION) -> role (_DECISION_ROLE).
    """
    return approver_role_for(_APPLIES_DECISION.get(applies_to, "testcase"))
