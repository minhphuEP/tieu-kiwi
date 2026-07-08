"""Confluence Cloud REST v2 fetcher.

`fetch_confluence(page_id, project_id, section_anchor=None)` is the entry point.
Fetches a page, upserts a BRD node (metadata only), and chunk-indexes the body
into Chroma so `search_kb` can retrieve it later. Idempotent via content_hash —
re-running on an unchanged page skips embedding.

Auth reuses JIRA_EMAIL + JIRA_API_TOKEN. Atlassian issues one credential per
account that works across Jira, Confluence, and other products.

Body format used: `atlas_doc_format` (JSON ADF). Converted to pretty-text by
`tieukiwi.adf.to_pretty_text` for chunking. Storage / view / export formats
would work too but ADF is what we already parse for Jira.
"""
import hashlib
import json

import httpx

from . import adf, config, db, rag


def _brd_ref(page_id):
    return f"CFL-{page_id}"


def _content_hash(text):
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _confluence_get(path):
    """Call Confluence REST v2. `path` is relative to /wiki (with leading /)."""
    if not (config.JIRA_BASE_URL and config.JIRA_EMAIL and config.JIRA_API_TOKEN):
        raise RuntimeError(
            "Confluence auth not configured. Set JIRA_BASE_URL, JIRA_EMAIL, "
            "and JIRA_API_TOKEN in .env (Atlassian uses one token across Jira "
            "and Confluence)."
        )
    url = f"{config.JIRA_BASE_URL.rstrip('/')}/wiki{path}"
    resp = httpx.get(
        url,
        auth=(config.JIRA_EMAIL, config.JIRA_API_TOKEN),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_page_metadata(page_id):
    """Cheap Confluence page metadata fetch (no body).

    Returns {"page_id", "version", "title"} or raises httpx.HTTPError / RuntimeError
    on failure. Used by hash-gate freshness check — 1 HTTP call per linked BRD
    when the Jira story hash is unchanged.
    """
    page = _confluence_get(f"/api/v2/pages/{page_id}")
    return {
        "page_id": str(page_id),
        "version": (page.get("version") or {}).get("number"),
        "title":   page.get("title") or "",
    }


def fetch_confluence(page_id, project_id=None, section_anchor=None):
    """Fetch one Confluence page → BRD node (Postgres) + chunks (Chroma).

    Args:
      page_id: numeric Confluence page id (as str or int).
      project_id: multi-tenant scope for both the BRD node and Chroma chunks.
                  Should be passed by the orchestrator (`ingest_jira_ticket`).
      section_anchor: URL fragment slug (e.g. "15.-Assign-new-creator...") if
                  the caller cares about a specific section. Stored on the BRD
                  node so agent can filter Chroma by section later.

    Returns:
      dict with `status` in {"ok", "cached", "error"} plus fields:
        node_id       int    id of the BRD node
        page_id       str
        title         str
        version       int
        url           str    full webui link (with #anchor if provided)
        chars         int    body char count
        chunks_indexed int   0 when status="cached"
    """
    page_id = str(page_id)
    ref = _brd_ref(page_id)

    # 1. Fetch page via REST v2 with ADF body
    try:
        page = _confluence_get(f"/api/v2/pages/{page_id}?body-format=atlas_doc_format")
    except httpx.HTTPStatusError as e:
        return {
            "tool": "fetch_confluence",
            "status": "error",
            "page_id": page_id,
            "error": f"Confluence API returned HTTP {e.response.status_code} for page {page_id}.",
        }
    except httpx.HTTPError as e:
        return {"tool": "fetch_confluence", "status": "error", "page_id": page_id,
                "error": f"Confluence request failed: {e}"}
    except RuntimeError as e:
        return {"tool": "fetch_confluence", "status": "error", "page_id": page_id, "error": str(e)}

    title = page.get("title") or ""
    version_num = (page.get("version") or {}).get("number")
    space_id = page.get("spaceId")

    # 2. Parse ADF body → pretty text
    body_val = ((page.get("body") or {}).get("atlas_doc_format") or {}).get("value")
    if isinstance(body_val, str):
        # v2 returns the ADF as a JSON-encoded string in `value`
        try:
            body_adf = json.loads(body_val)
        except json.JSONDecodeError:
            body_adf = None
    elif isinstance(body_val, dict):
        body_adf = body_val
    else:
        body_adf = None

    body_text = adf.to_pretty_text(body_adf) if body_adf else ""
    content_hash = _content_hash(body_text)

    # 3. Build full webui URL (with #anchor if provided)
    webui = ((page.get("_links") or {}).get("webui")
             or f"/spaces/_/pages/{page_id}/")
    full_url = f"{config.JIRA_BASE_URL.rstrip('/')}/wiki{webui}"
    if section_anchor:
        full_url += f"#{section_anchor}"

    # 4. Idempotency check: same hash → don't re-embed
    existing = db.get_node_by_ref("BRD", ref, project_id=project_id)
    if existing and existing["props_json"].get("content_hash") == content_hash:
        # Bump stored `version` so the hash-gate freshness check doesn't keep
        # re-firing when Confluence version drifted (edit-and-revert, whitespace,
        # etc.) without content change.
        if version_num and existing["props_json"].get("version") != version_num:
            db.update_node_props(ref, "version", version_num, type_="BRD")
        return {
            "tool": "fetch_confluence",
            "status": "cached",
            "node_id": existing["id"],
            "page_id": page_id,
            "title": title,
            "version": version_num,
            "url": full_url,
            "chars": len(body_text),
            "chunks_indexed": 0,
            "reason": "content_hash unchanged since last fetch",
        }

    # 5. Upsert BRD node with fresh metadata
    props = {
        "url": full_url,
        "title": title,
        "space_id": space_id,
        "page_id": page_id,
        "version": version_num,
        "section_anchor": section_anchor,
        "content_hash": content_hash,
        "content_preview": body_text[:500],
        "source": "confluence",
        "_meta": {
            "extraction_source": "confluence-rest",
            "confidence": 1.0,
            "source_file": full_url,
            "review_status": "verified",
        },
    }
    node_id = db.upsert_node_by_ref("BRD", ref, props, project_id=project_id)

    # 6. Chunk body + index into Chroma
    #    Metadata schema matches scripts/seed/kb.py convention so search_kb
    #    can filter by scope/project_id/doc_type as usual.
    chunks_indexed = 0
    if body_text.strip():
        meta = {
            "scope": "project" if project_id else "global",
            "project_id": project_id or "",
            "doc_type": "BRD",
            "source": "confluence",
            "page_id": page_id,
            "brd_ref": ref,
            "path": full_url,
        }
        # Delete stale chunks first. index_docs upserts by <ref>#c<i>, so a
        # shorter new body leaves the tail chunks of the old body lingering
        # in Chroma — search_kb would then retrieve text the PO already
        # removed from the PRD.
        rag.delete_by_parent_doc(ref)
        # rag.index_docs is heading-aware; each chunk gets its own `section` meta
        rag.index_docs([(ref, body_text, meta)])
        # Recount: rag doesn't return chunk count, but we can approximate.
        chunks_indexed = max(1, len(body_text) // 1000)

    return {
        "tool": "fetch_confluence",
        "status": "ok",
        "node_id": node_id,
        "page_id": page_id,
        "title": title,
        "version": version_num,
        "url": full_url,
        "chars": len(body_text),
        "chunks_indexed": chunks_indexed,
    }
