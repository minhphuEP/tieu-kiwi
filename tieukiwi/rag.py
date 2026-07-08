"""Chroma-backed RAG (Tier 1 shared KB).

Interface:
  chunk_text(text)  — split a document into retrievable chunks (heading-aware).
  index_docs(docs)  — docs: list[(id, text, metadata_dict)]. Idempotent (upsert).
                      Long docs are chunked before upsert; each chunk inherits the
                      source doc's metadata plus { parent_doc, chunk_index, section }.
  wipe()            — drop and recreate the collection. Call before re-seed after
                      changing the embedding model (existing vectors are incompatible).
  search(query, k, project_id, role, doc_type, include_global)
                    — semantic search with structural filters over metadata.

Embedding: Voyage AI `voyage-3` (multilingual, 1024 dim, 32k ctx). Set
VOYAGEAI_API_KEY in .env. Sign up at https://dash.voyageai.com/. The embedding
function is initialised lazily on first collection access so `import tieukiwi.rag`
works even when the key is unset (useful for unit-testing chunk_text alone).

Metadata schema populated by scripts/seed/kb.py (see infer_kb_metadata there):
  scope         : "project" | "global"
  project_id    : project code when scope="project"; absent when "global"
  role          : "QE" | "PO" | "BO" | "DEV"  (when the doc lives under a role dir)
  doc_type      : "template" | "sample" | "glossary" | "reference"
  applies_to    : ontology entity type or "General"  (from a small hard-coded map)
  source        : "skills" | "kb"
  parent_doc    : source document id (chunks share this)
  chunk_index   : position of this chunk within parent_doc
  section       : nearest markdown heading (## / ###), or "" when none
"""

import os
import re

import chromadb
from chromadb.utils.embedding_functions import VoyageAIEmbeddingFunction

_COLLECTION_NAME = "knowledge_base"
_VOYAGE_MODEL = "voyage-3"
_CHUNK_TARGET_CHARS = 1200
_CHUNK_OVERLAP_CHARS = 150

_client = chromadb.PersistentClient(path="./chroma_db")
_col = None


def _get_col():
    """Lazily initialise the Chroma collection bound to the Voyage embedding fn."""
    global _col
    if _col is None:
        api_key = os.environ.get("VOYAGEAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "VOYAGEAI_API_KEY is not set. Add it to .env "
                "(get one at https://dash.voyageai.com/) — Chroma embedding requires it."
            )
        embedding_fn = VoyageAIEmbeddingFunction(
            api_key=api_key, model_name=_VOYAGE_MODEL,
        )
        _col = _client.get_or_create_collection(
            _COLLECTION_NAME, embedding_function=embedding_fn,
        )
    return _col


_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)


def chunk_text(text):
    """Split a document into retrievable chunks.

    Cut at markdown ## / ### headings first (each heading starts a new section).
    A section longer than _CHUNK_TARGET_CHARS is windowed with _CHUNK_OVERLAP_CHARS
    overlap so no information vanishes at boundaries. Documents without any headings
    (e.g. PDF plain text) are windowed purely by length.

    Returns:
      list[(chunk_text, section_heading_or_empty)]
    """
    text = (text or "").strip()
    if not text:
        return []

    matches = list(_HEADING_RE.finditer(text))
    sections = []
    if not matches:
        sections.append((text, ""))
    else:
        prelude_end = matches[0].start()
        if prelude_end > 0:
            prelude = text[:prelude_end].strip()
            if prelude:
                sections.append((prelude, ""))
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            heading = m.group(2).strip()
            body = text[start:end].strip()
            if body:
                sections.append((body, heading))

    step = max(_CHUNK_TARGET_CHARS - _CHUNK_OVERLAP_CHARS, 1)
    chunks = []
    for body, heading in sections:
        if len(body) <= _CHUNK_TARGET_CHARS:
            chunks.append((body, heading))
            continue
        pos = 0
        while pos < len(body):
            piece = body[pos:pos + _CHUNK_TARGET_CHARS]
            if piece.strip():
                chunks.append((piece, heading))
            if pos + _CHUNK_TARGET_CHARS >= len(body):
                break
            pos += step
    return chunks


def index_docs(docs):
    """Idempotent upsert with heading-aware chunking.

    docs: list[(id, text, metadata_dict)]. Each doc is chunked; each chunk is
    upserted with id `<doc_id>#c<i>` and metadata inheriting the source doc's
    fields plus { parent_doc, chunk_index, section }.
    """
    if not docs:
        return
    all_ids, all_texts, all_metas = [], [], []
    for doc_id, text, meta in docs:
        for i, (chunk, section) in enumerate(chunk_text(text)):
            all_ids.append(f"{doc_id}#c{i}")
            all_texts.append(chunk)
            all_metas.append({
                **meta,
                "parent_doc": doc_id,
                "chunk_index": i,
                "section": section,
            })
    if not all_ids:
        return
    _get_col().upsert(ids=all_ids, documents=all_texts, metadatas=all_metas)


def wipe():
    """Delete the whole collection and recreate it empty on next access.

    Call this before re-running seed.py when:
      * source files were deleted (upsert alone leaves stale rows), or
      * the embedding model changed (existing vectors are incompatible).
    """
    global _col
    try:
        _client.delete_collection(_COLLECTION_NAME)
    except Exception:
        pass
    _col = None


def delete_by_parent_doc(parent_doc):
    """Remove every chunk whose metadata.parent_doc == parent_doc.

    Needed when a source doc SHRINKS: index_docs upserts chunk ids
    <parent_doc>#c0..N, so re-indexing a shorter body overwrites 0..M but
    leaves M+1..N behind as stale vectors. Call this before re-index to
    make Chroma reflect the current body exactly.
    """
    if not parent_doc:
        return
    try:
        _get_col().delete(where={"parent_doc": parent_doc})
    except Exception:
        pass


def search(query, k=4, project_id=None, role=None, doc_type=None, include_global=False):
    """Semantic search with structural filters.

    Args:
      query          natural-language query in Vietnamese or English
      k              max results
      project_id     if given, restrict to docs of this project
      role           if given ("QE" | "PO" | "BO" | "DEV"), restrict to that role
      doc_type       if given, restrict to that type (template|sample|glossary|reference)
      include_global if True AND project_id is set, also include docs with scope="global"
                     (typical fallback pattern: "give me my project's rules + shared ones")

    Returns:
      list[(id, text, metadata_dict)]
    """
    clauses = []
    if project_id:
        if include_global:
            clauses.append({"$or": [{"project_id": project_id}, {"scope": "global"}]})
        else:
            clauses.append({"project_id": project_id})
    if role:
        clauses.append({"role": role})
    if doc_type:
        clauses.append({"doc_type": doc_type})

    where = None
    if len(clauses) == 1:
        where = clauses[0]
    elif len(clauses) > 1:
        where = {"$and": clauses}

    res = _get_col().query(query_texts=[query], n_results=k, where=where)
    if not res["ids"] or not res["ids"][0]:
        return []
    return list(zip(res["ids"][0], res["documents"][0], res["metadatas"][0]))
