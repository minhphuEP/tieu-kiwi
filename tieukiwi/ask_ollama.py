"""Ollama variant of the tool-use loop — parallel to `tieukiwi.agent.ask()`.

Independent module so team's `agent.py` (Anthropic path) is untouched. Import
and call `ask_ollama()` when you want to run the loop against a local Ollama
model (qwen2.5:7b, llama3.1:8b, mistral-nemo, ...) to save Anthropic tokens.

Shares the same TOOLS registry + run_tool dispatcher as the Anthropic agent,
so tool surface is identical — only the underlying LLM changes.

Requires:
  - Ollama daemon at OLLAMA_HOST (default http://localhost:11434)
  - A tool-capable model pulled locally, e.g. `ollama pull qwen2.5:7b`

Ollama caveats (vs Claude):
  - Weaker tool selection: may pick wrong tool or fill wrong args
  - Multi-step reasoning weaker; keep `max_iters` low for safety
  - Only some models support tool calling (see Ollama docs)
"""
from __future__ import annotations

import json
import os

import httpx

from . import config
from .tools import TOOLS, run_tool


SYSTEM = """You are Tieu Kiwi, a QE (Quality Engineering) support agent for Crossian.

The team has a knowledge base (KB) that holds project-specific templates, samples,
review rules, and glossary. The KB is the source of truth for HOW artifacts should
look and WHAT team conventions apply. NEVER answer from your own training data when
the KB might have a project-specific version.

MANDATORY: Before answering any question about the following topics, you MUST call
the tool `search_kb` FIRST and base your answer only on what it returns:
  - test cases, test plans, or how tests should be structured / what fields they need
  - templates, samples, or the output format of any QE / PO / BO artifact
  - review rules, quality gates, definition-of-done
  - project-specific terminology, acronyms, glossary
  - team conventions ("how do we do X here")

When calling `search_kb`, set `doc_type` if the question clearly points at one type:
  template  → keywords: "template", "mẫu", "format", "khuôn"
  sample    → keywords: "ví dụ", "example", "sample"
  glossary  → keywords: "thuật ngữ", "từ vựng", "glossary"

Project scope (project_id) and role are injected server-side by the runtime — do NOT
try to pass them yourself, they will be ignored.

If `search_kb` returns nothing relevant, tell the user explicitly ("KB has no entry
for X") rather than falling back to generic knowledge.

Respond in the same language as the user (Vietnamese or English).
"""


def ask_ollama(
    user_msg,
    system=SYSTEM,
    project_id=None,
    role=None,
    model=None,
    max_iters=8,
):
    """Drive one tool-use conversation using Ollama; return the final text.

    Args:
      user_msg:   the user's question
      system:     system prompt
      project_id: multi-tenant scope (Slack layer sets this).
                  Forwarded to tools via context.
      role:       persona for RAG filtering (e.g. 'QE').
                  Forwarded to tools via context.
      model:      Ollama model name. Defaults to config.OLLAMA_LLM_MODEL.
      max_iters:  hard cap on tool-use iterations (safety for local LLM).
    """
    if model is None:
        model = config.OLLAMA_LLM_MODEL
    if not isinstance(model, str):
        raise TypeError(f"model must be a string, got {type(model).__name__}")

    context = {"project_id": project_id, "role": role}
    tools_payload = _tools_for_ollama()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    host = config.OLLAMA_HOST or os.getenv("OLLAMA_HOST", "http://localhost:11434")

    with httpx.Client(timeout=300.0) as http:
        for _ in range(max_iters):
            r = http.post(
                f"{host}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "tools": tools_payload,
                    "stream": False,
                    "options": {"temperature": 0.2},
                },
            )
            r.raise_for_status()
            msg = r.json()["message"]
            tool_calls = msg.get("tool_calls") or []

            # No tool call → final answer
            if not tool_calls:
                return msg.get("content", "").strip()

            # Keep the assistant turn (with tool_calls) in the transcript
            messages.append({
                "role": "assistant",
                "content": msg.get("content", ""),
                "tool_calls": tool_calls,
            })

            # Execute each tool call and feed results back
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name")
                raw_args = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        raw_args = {}
                try:
                    out = run_tool(name, raw_args, context=context)
                except Exception as e:
                    out = f"[tool_error] {e}"
                messages.append({
                    "role": "tool",
                    "content": _json_or_str(out),
                    "name": name,
                })
    return "[warn] ask_ollama: max_iters reached without final answer"


# --- helpers ---------------------------------------------------------------

def _tools_for_ollama():
    """Convert TOOLS (Anthropic input_schema shape) to Ollama's function shape."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOLS
    ]


def _json_or_str(obj):
    """Serialise tool output for the LLM: JSON when possible, else str()."""
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(obj)


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "Is FRONT-3494 ready to go live?"
    print(ask_ollama(q))
