"""Seed the Chroma RAG (Tier 1 shared KB) from local markdown folders.

Idempotent by default (uses `col.upsert()` under the hood): re-running against
the same source files is safe. Use `--wipe` when source files were deleted and
you want the collection to reflect current disk state exactly.

Metadata inferred from the file path — no manual tagging needed:

    kb/<PROJECT_ID>/...                     scope=project, project_id=<PROJECT_ID>
    kb/_global/...                          scope=global
    kb/*/QE|PO|BO|DEV/...                   role=<that role>
    kb/**/templates/**                      doc_type=template
    kb/**/samples/**                        doc_type=sample
    kb/**/*glossary*                        doc_type=glossary
    (else)                                  doc_type=reference

Usage:
    python scripts/seed/kb.py               # upsert (safe re-run)
    python scripts/seed/kb.py --wipe        # wipe collection then re-seed from scratch
"""
import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[2]))

from dotenv import load_dotenv; load_dotenv()

import argparse
from pathlib import Path

from tieukiwi.rag import index_docs, wipe as wipe_collection
from tieukiwi.text_extract import read_text, SUPPORTED_EXTS

# Directories to ingest, in the order they should be indexed.
BASE_DIRS = ["skills", "kb"]

# File extensions we pick up. Text is extracted via tieukiwi.text_extract, so
# adding a new format = teaching text_extract, not editing this file.
_EXT_GLOBS = tuple(f"*{ext}" for ext in SUPPORTED_EXTS)

# Small map for legacy `skills/` files that don't fit the new folder convention.
APPLIES_TO = {
    "test-driven-development": "TestCase",
    "code-review-and-quality": "Bug",
    "spec-driven-development": "Requirement",
}

# Roles recognised when they appear as the SECOND path segment under kb/*/.
ROLES = {"QE", "PO", "BO", "DEV"}


def infer_kb_metadata(path: Path) -> dict:
    """Parse kb/<PROJECT_ID | _global>/<ROLE?>/.../<file>.md into metadata."""
    parts = path.parts
    if len(parts) < 3 or parts[0] != "kb":
        return {"scope": "global", "doc_type": "reference"}

    first = parts[1]
    meta = {"scope": "global" if first == "_global" else "project"}
    if first != "_global":
        meta["project_id"] = first

    # role dir: kb/<project|_global>/<ROLE>/...
    if len(parts) > 3 and parts[2] in ROLES:
        meta["role"] = parts[2]

    # doc_type inference from path segments / filename
    if "templates" in parts:
        meta["doc_type"] = "template"
    elif "samples" in parts:
        meta["doc_type"] = "sample"
    elif "glossary" in path.stem.lower():
        meta["doc_type"] = "glossary"
    else:
        meta["doc_type"] = "reference"

    return meta


def collect_docs():
    """Walk BASE_DIRS and return list of (id, text, metadata) tuples.

    Any file with an extension supported by tieukiwi.text_extract is picked up
    (.md .txt .pdf .docx .doc). Text is extracted uniformly.

    IDs are path-based (colon-separated) so files in different folders / formats
    never collide. Example: `kb/CDM/glossary.md` -> `kb:CDM:glossary`.
    """
    docs = []
    for base in BASE_DIRS:
        base_path = Path(base)
        if not base_path.exists():
            continue

        # Collect all supported files across extensions, dedupe by path
        files = set()
        for pattern in _EXT_GLOBS:
            files.update(base_path.rglob(pattern))

        for f in sorted(files):
            # Drop extension for a stable id: kb/CDM/glossary.md -> kb:CDM:glossary
            doc_id = str(f.with_suffix("")).replace("/", ":")
            try:
                text = read_text(f)
            except Exception as e:
                print(f"[warn] skip {f}: {e}")
                continue
            if not text.strip():
                print(f"[warn] skip {f}: no extractable text")
                continue

            metadata = {
                "source": base,
                "path": str(f),
                "format": f.suffix.lower().lstrip("."),
                "applies_to": APPLIES_TO.get(f.stem, "General"),
            }
            if base == "kb":
                metadata.update(infer_kb_metadata(f))
            else:
                # skills/ docs are role-agnostic, project-agnostic knowledge —
                # mark them scope="global" so rag.search(..., project_id=X,
                # include_global=True) can still find them (its $or clause
                # requires either a matching project_id or scope="global").
                metadata["scope"] = "global"

            docs.append((doc_id, text, metadata))
    return docs


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument(
        "--wipe", action="store_true",
        help="Delete the collection and recreate it empty before seeding.",
    )
    args = ap.parse_args()

    if args.wipe:
        wipe_collection()
        print("[info] Wiped 'knowledge_base' collection.")

    docs = collect_docs()
    if not docs:
        print("[warn] No .md files found in skills/ and kb/.")
        return
    index_docs(docs)
    print(f"[ok] Upserted {len(docs)} docs into Chroma.")
    for doc_id, _, meta in docs:
        # Show the interesting metadata fields
        bits = []
        for key in ("scope", "project_id", "role", "doc_type"):
            if key in meta:
                bits.append(f"{key}={meta[key]}")
        print(f"       - {doc_id:50s} {' '.join(bits)}")


if __name__ == "__main__":
    main()
