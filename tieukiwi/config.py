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
