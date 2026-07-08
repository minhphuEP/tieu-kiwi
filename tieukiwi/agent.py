import os
from anthropic import Anthropic
from . import config
from .tools import TOOLS, run_tool

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


_DEFAULT_SYSTEM = (
    "You are Tieu Kiwi, a QE support agent. Only state specific facts about "
    "tickets, storage, or system internals (status, caching, ingestion, etc.) "
    "when a tool call actually returned that information — never invent or "
    "assume such details."
)


def ask(user_msg, system=_DEFAULT_SYSTEM,
        project_id=None, role=None, model = None):
    """Drive one tool-use conversation to completion and return the final text.

    Args:
      user_msg:   the user's question
      system:     system prompt
      project_id: multi-tenant scope. When set, every tool call is auto-scoped
                  to this project by run_tool. Callers should set this via the
                  Slack layer (channel_id -> project_id).
      role:       persona for RAG filtering (e.g. 'QE'). Passed to search_kb.
    """
    context = {"project_id": project_id, "role": role}
    if model is None:
        model = config.model_for("agent")
    if not isinstance(model, str):
        raise TypeError(
            f"model must be a string, got {type(model).__name__}. "
            f"Did you pass the config module by mistake? Use config.DEFAULT_MODEL "
            f"or config.model_for('agent')."
        )
    messages = [{"role": "user", "content": user_msg}]
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
                out = run_tool(block.name, block.input, context=context)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(out),
                })
        messages.append({"role": "user", "content": results})