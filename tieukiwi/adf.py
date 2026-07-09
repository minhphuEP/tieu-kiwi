"""Atlassian Document Format (ADF) parsers used by ingest_jira_ticket / fetch_confluence.

ADF is Atlassian's JSON tree format for rich text — issue descriptions,
Confluence bodies, comments. Docs: https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/

We only need three views:
  - to_text(node)          → flat plain text (was tools._adf_to_text)
  - extract_urls(node)     → list of URLs (smartlinks + text-link marks)
  - extract_tables(node)   → list of tables, each as list of rows, each row as
                             list of cell plain texts

Not a general-purpose ADF library — just enough for our two ingest paths.
"""
import re
from urllib.parse import urlparse


# --- text -----------------------------------------------------------------

def to_text(node):
    """Flatten an ADF node (dict / list / str / None) to a plain string.

    Preserves whitespace but not structure. For richer output (markdown-ish)
    with line breaks between paragraphs / rows, use `to_pretty_text`.
    """
    if node is None:
        return None
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return "".join(t for t in (to_text(c) for c in node.get("content", [])) if t)
    if isinstance(node, list):
        return "".join(to_text(n) or "" for n in node)
    return None


def to_pretty_text(node, _depth=0):
    """Flatten ADF with line breaks between block-level nodes and pipe-separated
    cells inside table rows. Enough for LLM prompts to see structure."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(to_pretty_text(n, _depth) for n in node)
    if not isinstance(node, dict):
        return ""

    t = node.get("type")
    content = node.get("content") or []

    if t == "text":
        return node.get("text", "")
    if t in ("paragraph", "heading"):
        prefix = "#" * (node.get("attrs", {}).get("level", 1)) + " " if t == "heading" else ""
        return prefix + "".join(to_pretty_text(c, _depth + 1) for c in content) + "\n\n"
    if t == "hardBreak":
        return "\n"
    if t == "bulletList" or t == "orderedList":
        return "".join(to_pretty_text(c, _depth + 1) for c in content)
    if t == "listItem":
        return "- " + "".join(to_pretty_text(c, _depth + 1) for c in content).rstrip() + "\n"
    if t == "table":
        rows = [to_pretty_text(r, _depth + 1) for r in content]
        return "\n".join(rows) + "\n\n"
    if t in ("tableRow",):
        cells = [to_pretty_text(c, _depth + 1).replace("\n", " ").strip() for c in content]
        return "| " + " | ".join(cells) + " |"
    if t in ("tableHeader", "tableCell"):
        return "".join(to_pretty_text(c, _depth + 1) for c in content).strip()
    # inlineCard / blockCard: keep the URL as literal text so it's not lost
    if t in ("inlineCard", "blockCard"):
        url = (node.get("attrs") or {}).get("url", "")
        return url
    if t == "mention":
        return "@" + (node.get("attrs") or {}).get("text", "")
    if t == "emoji":
        return (node.get("attrs") or {}).get("shortName", "")
    # Fallback: recurse into children
    return "".join(to_pretty_text(c, _depth + 1) for c in content)


# --- URL extraction -------------------------------------------------------

_SMARTLINK_TYPES = {"inlineCard", "blockCard"}

def extract_urls(node):
    """Walk the ADF tree and collect every URL: smartlinks, block cards, and
    text-link marks. Returns a de-duplicated list, preserving first-seen order.
    """
    seen = []
    _walk_urls(node, seen)
    # Preserve order, drop dupes
    out, marked = [], set()
    for u in seen:
        if u and u not in marked:
            marked.add(u)
            out.append(u)
    return out


def _walk_urls(node, out):
    if node is None:
        return
    if isinstance(node, list):
        for n in node:
            _walk_urls(n, out)
        return
    if not isinstance(node, dict):
        return
    t = node.get("type")
    attrs = node.get("attrs") or {}
    if t in _SMARTLINK_TYPES:
        u = attrs.get("url")
        if u:
            out.append(u)
    if t == "text":
        # Text nodes carry link marks: marks: [{type:"link", attrs:{href:...}}]
        for m in node.get("marks") or []:
            if m.get("type") == "link":
                href = (m.get("attrs") or {}).get("href")
                if href:
                    out.append(href)
    for c in node.get("content") or []:
        _walk_urls(c, out)


# --- Confluence URL parsing ----------------------------------------------

# Confluence page URLs come in a few shapes. Extract the numeric page ID from
# any of them, or None if this isn't a Confluence page URL at all.
#
#   https://<site>.atlassian.net/wiki/spaces/<space>/pages/2541551769/Title#anchor
#   https://<site>.atlassian.net/wiki/spaces/<space>/pages/2541551769
#   https://<site>.atlassian.net/wiki/x/Fc1bBw   ← tiny link (harder — needs API resolution)
_PAGE_ID_RE = re.compile(r"/wiki/spaces/[^/]+/pages/(\d+)")


def parse_confluence_url(url):
    """Return {"page_id": str, "section_anchor": str|None, "host": str} or None
    if the URL is not a recognisable Confluence page URL."""
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.netloc.endswith(".atlassian.net"):
        return None
    m = _PAGE_ID_RE.search(parsed.path or "")
    if not m:
        return None
    return {
        "page_id": m.group(1),
        "section_anchor": parsed.fragment or None,
        "host": parsed.netloc,
    }


# --- Table extraction (for [Bug] subtask parsing) -------------------------

def extract_tables(node):
    """Return a list of tables. Each table = list of rows; each row = list of
    cell plain-text strings. Header row (tableHeader cells) is included as
    row 0 — caller decides whether to treat it as headers.
    """
    tables = []
    _walk_tables(node, tables)
    return tables


def _walk_tables(node, out):
    if node is None:
        return
    if isinstance(node, list):
        for n in node:
            _walk_tables(n, out)
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "table":
        rows = []
        for row_node in node.get("content") or []:
            if row_node.get("type") != "tableRow":
                continue
            cells = []
            for cell_node in row_node.get("content") or []:
                if cell_node.get("type") not in ("tableHeader", "tableCell"):
                    continue
                cells.append(to_text(cell_node) or "")
            rows.append(cells)
        if rows:
            out.append(rows)
        # Don't recurse into the table (nested tables are rare and we don't need them)
        return
    for c in node.get("content") or []:
        _walk_tables(c, out)
