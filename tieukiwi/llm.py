"""LLM abstraction for ingestion pipelines.

Kept SEPARATE from tieukiwi/agent.py (which talks to Anthropic directly for
the tool-use loop) so ingestion can be pointed at Ollama for offline dev,
or Claude Sonnet when the API key is available.

Interface:
  complete(prompt, system=None, max_tokens=2048, temperature=0.2,
           json_mode=False) -> str
  complete_json(prompt, system=None, **kw)                 -> dict
"""
from __future__ import annotations
import json
import os

import httpx

from .config import (
    LLM_PROVIDER,
    ANTHROPIC_MODEL,
    OLLAMA_HOST,
    OLLAMA_LLM_MODEL,
)


def complete(prompt, system=None, max_tokens=2048, temperature=0.2, json_mode=False):
    """Text completion. If json_mode, ask the provider to return JSON only."""
    if LLM_PROVIDER == "anthropic":
        return _anthropic_complete(prompt, system, max_tokens, temperature, json_mode)
    if LLM_PROVIDER == "ollama":
        return _ollama_complete(prompt, system, max_tokens, temperature, json_mode)
    raise ValueError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER}")


def complete_json(prompt, system=None, **kw):
    """Convenience: run complete() in JSON mode and parse."""
    raw = complete(prompt, system=system, json_mode=True, **kw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # LLM sometimes wraps in ```json ... ``` fences even when told not to.
        cleaned = _strip_code_fence(raw)
        return json.loads(cleaned)


# --- providers -------------------------------------------------------------

def _anthropic_complete(prompt, system, max_tokens, temperature, json_mode):
    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    kwargs = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    sys_prompt = system or ""
    if json_mode:
        sys_prompt += (
            "\n\nRespond with valid JSON only. No prose, no markdown fences, "
            "no leading/trailing whitespace outside the JSON."
        )
    if sys_prompt:
        kwargs["system"] = sys_prompt.strip()
    msg = client.messages.create(**kwargs)
    return msg.content[0].text.strip()


def _ollama_complete(prompt, system, max_tokens, temperature, json_mode):
    payload = {
        "model": OLLAMA_LLM_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if system:
        payload["system"] = system
    if json_mode:
        payload["format"] = "json"
    with httpx.Client(timeout=180.0) as client:
        r = client.post(f"{OLLAMA_HOST}/api/generate", json=payload)
        r.raise_for_status()
        return r.json()["response"].strip()


def _strip_code_fence(s):
    """Remove ```json ... ``` fences the model sometimes adds despite instruction."""
    s = s.strip()
    if s.startswith("```"):
        # remove first fence line and trailing fence
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


if __name__ == "__main__":
    print(f"provider={LLM_PROVIDER}")
    out = complete("Say hi in Vietnamese in 5 words.", max_tokens=50)
    print(f"out={out!r}")
