import os
from anthropic import Anthropic
from . import config
from .tools import TOOLS, run_tool

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def ask(user_msg, system="You are Tieu Kiwi, a QE support agent.", model=None):
    if model is None:
        model = config.model_for("agent")
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
                out = run_tool(block.name, block.input)
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(out),
                })
        messages.append({"role": "user", "content": results})