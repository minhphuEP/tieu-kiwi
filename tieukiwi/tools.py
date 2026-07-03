import httpx

from . import config, db, rag


# --- Layer A skeletons (TODO: implement) ---
# These tools are registered so the agent knows they exist, but the generation /
# integration logic is not built yet. Fill these in during the Layer A build-out.

def _not_implemented(tool, todo):
    return {"tool": tool, "status": "not_implemented", "todo": todo}


def gen_testcase(requirement_ref):
    model = config.model_for("gen_testcase")  # TODO: pass into the Claude call when implemented
    # TODO: load requirement + ACs (db.trace) and relevant KB (rag.search),
    # then call the Claude API (model=model) to draft TestCase nodes; return proposed testcases.
    return _not_implemented(
        "gen_testcase", "Generate testcases via Claude from requirement + KB context."
    )


def gen_test_plan(requirement_ref):
    model = config.model_for("gen_test_plan")  # TODO: pass into the Claude call when implemented
    # TODO: aggregate ACs/testcases for the requirement and draft a structured test plan (model=model).
    return _not_implemented(
        "gen_test_plan", "Generate a structured test plan via Claude."
    )


def gen_critic(text):
    model = config.model_for("gen_critic")  # TODO: pass into the Claude call when implemented
    # TODO: critique PRD/Design/spec text against KB review rules (rag.search) via Claude (model=model).
    return _not_implemented(
        "gen_critic", "Critique PRD/Design against KB review rules via Claude."
    )


def _adf_to_text(node):
    # Best-effort flatten of Atlassian Document Format (or a plain string) to text.
    if node is None:
        return None
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return "".join(t for t in (_adf_to_text(c) for c in node.get("content", [])) if t)
    if isinstance(node, list):
        return "".join(_adf_to_text(n) or "" for n in node)
    return None


def _story_points(fields):
    # Story points live in a custom field whose id varies per Jira instance.
    # Try the common ids and return the first numeric value; skip if none present.
    for k in ("customfield_10016", "customfield_10026", "customfield_10004", "customfield_10002"):
        v = fields.get(k)
        if isinstance(v, (int, float)):
            return v
    return None


def fetch_jira(issue_key):
    # Read a Jira Cloud issue (REST v3) and upsert it into the graph as a Requirement.
    if not (config.JIRA_BASE_URL and config.JIRA_EMAIL and config.JIRA_API_TOKEN):
        return {
            "tool": "fetch_jira",
            "status": "error",
            "error": "Jira is not configured. Set JIRA_BASE_URL, JIRA_EMAIL, and "
                     "JIRA_API_TOKEN in .env (see .env.example).",
        }

    url = f"{config.JIRA_BASE_URL.rstrip('/')}/rest/api/3/issue/{issue_key}"
    try:
        resp = httpx.get(
            url,
            auth=(config.JIRA_EMAIL, config.JIRA_API_TOKEN),
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return {
            "tool": "fetch_jira",
            "status": "error",
            "error": f"Jira API returned HTTP {e.response.status_code} for {issue_key}.",
        }
    except httpx.HTTPError as e:
        return {"tool": "fetch_jira", "status": "error", "error": f"Jira request failed: {e}"}

    data = resp.json()
    fields = data.get("fields", {}) or {}
    key = data.get("key", issue_key)
    summary = fields.get("summary")
    description = _adf_to_text(fields.get("description"))
    issuetype = (fields.get("issuetype") or {}).get("name")
    status = (fields.get("status") or {}).get("name")
    priority = (fields.get("priority") or {}).get("name")

    # People: assignee may be JSON null -> "Unassigned". Guard every level with `or {}`.
    assignee = (fields.get("assignee") or {}).get("displayName") or "Unassigned"
    reporter = (fields.get("reporter") or {}).get("displayName")

    # Optional fields — vary per Jira instance; never fail if absent.
    fix_versions = [v.get("name") for v in (fields.get("fixVersions") or []) if v.get("name")]
    story_points = _story_points(fields)

    props = {
        "source": "jira",
        "summary": summary,
        "status": status,
        "issuetype": issuetype,
        "priority": priority,
        "assignee": assignee,
        "reporter": reporter,
        "description": description,
    }
    if fix_versions:
        props["fix_versions"] = fix_versions
    if story_points is not None:
        props["story_points"] = story_points

    node_id = db.upsert_node_by_ref("Requirement", key, props)

    issue = {
        "key": key,
        "summary": summary,
        "issuetype": issuetype,
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "reporter": reporter,
    }
    if fix_versions:
        issue["fix_versions"] = fix_versions
    if story_points is not None:
        issue["story_points"] = story_points

    return {"tool": "fetch_jira", "status": "ok", "issue": issue, "node_id": node_id}


TOOLS = [
  {
    "name": "search_kb",
    "description": "Find relevant rules/glossary/rubrics in the KB.",
    "input_schema": {
      "type": "object",
      "properties": {"query": {"type": "string"}},
      "required": ["query"],
    },
  },
  {
    "name": "coverage_gap",
    "description": "List AcceptanceCriterion items not yet covered by any TestCase.",
    "input_schema": {"type": "object", "properties": {}},
  },
  {
    "name": "go_no_go",
    "description": (
        "Assess whether a requirement/feature is ready to go live in production; "
        "returns a GO/NO-GO decision plus the actions needed if NO-GO."
    ),
    "input_schema": {
      "type": "object",
      "properties": {"requirement_ref": {"type": "string"}},
      "required": ["requirement_ref"],
    },
  },
  {
    "name": "trace",
    "description": (
        "Trace a requirement's path: Requirement -> AcceptanceCriteria -> TestCases "
        "-> TestRuns -> Bugs, showing which AC is covered and its pass/fail status."
    ),
    "input_schema": {
      "type": "object",
      "properties": {"requirement_ref": {"type": "string"}},
      "required": ["requirement_ref"],
    },
  },
  {
    "name": "bug_blast_radius",
    "description": (
        "Estimate a bug's blast radius: how many Requirements/AcceptanceCriteria depend "
        "on the Component(s) it affects, and a derived priority (P1-P4)."
    ),
    "input_schema": {
      "type": "object",
      "properties": {"bug_ref": {"type": "string"}},
      "required": ["bug_ref"],
    },
  },
  {
    "name": "gen_testcase",
    "description": "Generate test cases for a requirement/AC. (SKELETON — TODO: implement LLM generation.)",
    "input_schema": {
      "type": "object",
      "properties": {"requirement_ref": {"type": "string"}},
      "required": ["requirement_ref"],
    },
  },
  {
    "name": "gen_test_plan",
    "description": "Generate a test plan for a requirement. (SKELETON — TODO: implement LLM generation.)",
    "input_schema": {
      "type": "object",
      "properties": {"requirement_ref": {"type": "string"}},
      "required": ["requirement_ref"],
    },
  },
  {
    "name": "gen_critic",
    "description": "Critique a PRD/Design/spec and flag issues against KB rules. (SKELETON — TODO.)",
    "input_schema": {
      "type": "object",
      "properties": {"text": {"type": "string"}},
      "required": ["text"],
    },
  },
  {
    "name": "fetch_jira",
    "description": (
        "Fetch a Jira issue by key (e.g. PROJ-123) from Jira Cloud and store it in the "
        "graph as a Requirement node. Returns key, summary, issuetype, status, priority, "
        "assignee (or 'Unassigned'), reporter (+ optional fix_versions / story_points) and node id."
    ),
    "input_schema": {
      "type": "object",
      "properties": {"issue_key": {"type": "string"}},
      "required": ["issue_key"],
    },
  },
]

def run_tool(name, args):
    if name == "search_kb":
        return rag.search(args["query"])
    if name == "coverage_gap":
        return db.coverage_gap()
    if name == "go_no_go":
        return db.go_no_go(args["requirement_ref"])
    if name == "trace":
        return db.trace(args["requirement_ref"])
    if name == "bug_blast_radius":
        return db.bug_blast_radius(args["bug_ref"])
    if name == "gen_testcase":
        return gen_testcase(args["requirement_ref"])
    if name == "gen_test_plan":
        return gen_test_plan(args["requirement_ref"])
    if name == "gen_critic":
        return gen_critic(args["text"])
    if name == "fetch_jira":
        return fetch_jira(args["issue_key"])
    raise ValueError(f"Unknown tool: {name}")