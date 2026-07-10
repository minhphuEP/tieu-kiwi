import os
from anthropic import Anthropic
from . import config
from .tools import TOOLS, run_tool

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


DEFAULT_SYSTEM = """You are Tieu Kiwi, a QE support agent.

Rules for answering questions about tickets / requirements / BRDs / test coverage:

- When the user names a ticket (e.g. CDM-199, CDM-263, CDM-286), ALWAYS call
  `get_ticket(ref)` FIRST. It's polymorphic — it handles Requirement, Bug,
  TestRun, UserStory, and BRD refs. Never answer from memory or guess type.

- If `get_ticket` returns `found=False`, OR if `warnings` mention missing
  data (0 AC, no BRD linked, etc.), call `ingest_jira_ticket(ref)` to pull
  from Jira, THEN call `get_ticket` again.

- Whenever a tool returns a `warnings` array, ECHO EVERY WARNING verbatim
  to the user. They flag missing data the user must know about.

- NEVER invent ACs, TestCases, Bugs, or IDs that are not in the tool return
  values. If the tool returns 0 items, say so explicitly ("chưa có AC nào
  trong graph"), never list AC1/AC2/AC4 etc. from your own imagination.
  Only state specific facts about tickets, storage, or system internals
  (status, caching, ingestion, etc.) when a tool call actually returned that
  information.

- Use `fetch_confluence` ONLY when the user pastes a Confluence URL directly
  outside of a Jira context — `ingest_jira_ticket` already handles Confluence
  URLs found in Jira descriptions.

- Answer in the language the user asked in (Vietnamese or English).
"""


def ask(user_msg, system=None, project_id=None, role=None, model=None, on_step=None):
    if system is None:
        system = DEFAULT_SYSTEM
    """Drive one tool-use conversation to completion and return the final text.

    Args:
      user_msg:   the user's question
      system:     system prompt
      project_id: multi-tenant scope. When set, every tool call is auto-scoped
                  to this project by run_tool. Callers should set this via the
                  Slack layer (channel_id -> project_id).
      role:       persona for RAG filtering (e.g. 'QE'). Passed to search_kb.
      on_step:    optional callable receiving event dicts for live progress
                  (see tieukiwi.progress.label_for). Fired on model
                  thinking/tool decisions. Exceptions from the callback are
                  swallowed so a broken UI never breaks the agent.
    """
    context = {"project_id": project_id, "role": role, "on_step": on_step}
    if model is None:
        model = config.model_for("agent")
    if not isinstance(model, str):
        raise TypeError(
            f"model must be a string, got {type(model).__name__}. "
            f"Did you pass the config module by mistake? Use config.DEFAULT_MODEL "
            f"or config.model_for('agent')."
        )

    def _emit(ev):
        if on_step is None:
            return
        try:
            on_step(ev)
        except Exception:
            pass

    messages = [{"role": "user", "content": user_msg}]
    _emit({"phase": "thinking"})
    while True:
        resp = client.messages.create(
            model=model, max_tokens=2000, system=system,
            tools=TOOLS, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")

        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                _emit({"phase": "tool_start", "name": block.name, "args": block.input})
                out = run_tool(block.name, block.input, context=context)
                _emit({"phase": "tool_done", "name": block.name})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(out),
                })
        messages.append({"role": "user", "content": results})
        _emit({"phase": "thinking"})