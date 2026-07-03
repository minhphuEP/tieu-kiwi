"""Central config: load .env once and expose settings.

Importing this module runs load_dotenv(), so any module that reads config through
here gets the .env values even when invoked directly (e.g. python -c "..."),
not only via cli.py.

Use os.getenv (returns None) rather than os.environ[...] (raises KeyError) so a
missing value surfaces as a clear error at the point of use.
"""

import os

from dotenv import load_dotenv

# Load .env once, on first import of this module.
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# ---- Ingestion LLM (see tieukiwi/llm.py) --------------------------------
# LLM used by ingestion pipelines to extract entities from BRD/PDF/etc.
# The agent's tool-use loop (agent.py) still talks to Anthropic directly.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()

# Anthropic (default provider when key is set)
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Ollama fallback (offline / dev without key)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_LLM_MODEL = os.getenv("OLLAMA_LLM_MODEL", "qwen2.5:7b")

# --- Model configuration ---
# Default model for the agent; override with ANTHROPIC_MODEL in .env.
DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# Per-task model overrides. Each falls back to DEFAULT_MODEL when its env var is unset.
TASK_MODELS = {
    "agent":         os.getenv("MODEL_AGENT", DEFAULT_MODEL),
    "gen_critic":    os.getenv("MODEL_GEN_CRITIC", DEFAULT_MODEL),
    "gen_testcase":  os.getenv("MODEL_GEN_TESTCASE", DEFAULT_MODEL),
    "gen_test_plan": os.getenv("MODEL_GEN_TEST_PLAN", DEFAULT_MODEL),
}


def model_for(task):
    """Return the model configured for a task, or DEFAULT_MODEL if none."""
    return TASK_MODELS.get(task, DEFAULT_MODEL)


# --- Jira (data source read through the fetch_jira tool) ---
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

# --- Slack (Layer B, Socket Mode) ---
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
