"""Chroma-backed RAG (Tier 1 shared KB).

Interface:
  index_docs(docs)  — docs: list[(id, text, metadata_dict)]. Idempotent (upsert).
  wipe()            — drop and recreate the collection. Use before re-seed when
                      source files were deleted (upsert alone leaves stale rows).
  search(query, k, project_id, role, doc_type, include_global)
                    — semantic search with structural filters over metadata.

Chroma downloads its default embedding model (`all-MiniLM-L6-v2`, ~80 MB) into
`~/.cache/chroma/` on first use. Vector store lives at `./chroma_db/`.

Metadata schema populated by seed.py (see infer_kb_metadata there):
  scope         : "project" | "global"
  project_id    : project code when scope="project"; absent when "global"
  role          : "QE" | "PO" | "BO" | "DEV"  (when the doc lives under a role dir)
  doc_type      : "template" | "sample" | "glossary" | "reference"
  applies_to    : ontology entity type or "General"  (from a small hard-coded map)
  source        : "skills" | "kb"
"""

import chromadb

_COLLECTION_NAME = "knowledge_base"

client = chromadb.PersistentClient(path="./chroma_db")
col = client.get_or_create_collection(_COLLECTION_NAME)


def index_docs(docs):
    """Idempotent upsert. Safe to call repeatedly; existing IDs are overwritten."""
    if not docs:
        return
    col.upsert(
        ids=[d[0] for d in docs],
        documents=[d[1] for d in docs],
        metadatas=[d[2] for d in docs],
    )


def wipe():
    """Delete the whole collection and recreate it empty.

    Call this before re-running seed.py when source files were deleted — otherwise
    stale chunks linger and the agent may retrieve rules for files that no longer
    exist on disk.
    """
    global col
    client.delete_collection(_COLLECTION_NAME)
    col = client.get_or_create_collection(_COLLECTION_NAME)


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

    res = col.query(query_texts=[query], n_results=k, where=where)
    if not res["ids"] or not res["ids"][0]:
        return []
    return list(zip(res["ids"][0], res["documents"][0], res["metadatas"][0]))
