from . import db, rag


# --- Layer A skeletons (TODO: implement) ---
# These tools are registered so the agent knows they exist, but the generation /
# integration logic is not built yet. Fill these in during the Layer A build-out.

def _not_implemented(tool, todo):
    return {"tool": tool, "status": "not_implemented", "todo": todo}


def gen_testcase(requirement_ref):
    # TODO: load requirement + ACs (db.trace) and relevant KB (rag.search),
    # then call the Claude API to draft TestCase nodes; return proposed testcases.
    return _not_implemented(
        "gen_testcase", "Generate testcases via Claude from requirement + KB context."
    )


def gen_test_plan(requirement_ref):
    # TODO: aggregate ACs/testcases for the requirement and draft a structured test plan.
    return _not_implemented(
        "gen_test_plan", "Generate a structured test plan via Claude."
    )


def gen_critic(text):
    # TODO: critique PRD/Design/spec text against KB review rules (rag.search) via Claude.
    return _not_implemented(
        "gen_critic", "Critique PRD/Design against KB review rules via Claude."
    )


def fetch_jira(issue_key):
    # TODO: call the Jira REST API with httpx (Basic auth: email + API token from .env).
    return _not_implemented(
        "fetch_jira", "Fetch a Jira issue via httpx REST (Basic auth from .env)."
    )


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
    "description": "Fetch a Jira issue (requirement/ticket) by key. (SKELETON — TODO: httpx REST call.)",
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