"""Ask routing: map an entity type to the owner role responsible for it.

Used to route coverage gaps / go-no-go action items to the right person.
The Slack delivery part (actually posting to the owner) is TODO (Layer B).
"""

# Entity type (per ontology) -> owner role responsible for fixing gaps on it.
ENTITY_OWNER = {
    "Requirement": "product-owner",
    "AcceptanceCriterion": "qa-lead",
    "TestCase": "qa-engineer",
    "TestPlan": "qa-lead",
    "TestRun": "qa-engineer",
    "Bug": "dev-owner",
    "Component": "tech-lead",
}

DEFAULT_OWNER = "qa-lead"


def owner_for(entity_type):
    return ENTITY_OWNER.get(entity_type, DEFAULT_OWNER)


def route_gap(entity_type, ref, note=None):
    # Build a routing record for a gap/action item.
    # TODO (Layer B): deliver this to the owner via Slack (Bolt, chat:write).
    return {
        "entity_type": entity_type,
        "ref": ref,
        "owner": owner_for(entity_type),
        "note": note,
    }
