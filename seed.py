from dotenv import load_dotenv; load_dotenv()

from pathlib import Path
from tieukiwi.rag import index_docs

# Directories to ingest
BASE_DIRS = ["skills", "kb"]

# Map applies_to for specific skills (id = filename without extension)
APPLIES_TO = {
    "test-driven-development": "TestCase",
    "code-review-and-quality": "Bug",
    "spec-driven-development": "Requirement",
}


def collect_docs():
    docs = []
    for base in BASE_DIRS:
        base_path = Path(base)
        if not base_path.exists():
            continue
        for md in sorted(base_path.rglob("*.md")):
            doc_id = md.stem
            text = md.read_text(encoding="utf-8")
            metadata = {
                "source": base,
                "applies_to": APPLIES_TO.get(doc_id, "General"),
            }
            docs.append((doc_id, text, metadata))
    return docs


def main():
    docs = collect_docs()
    if not docs:
        print("No .md files found in skills/ and kb/.")
        return
    index_docs(docs)
    print(f"Ingested {len(docs)} docs into Chroma.")


if __name__ == "__main__":
    main()
