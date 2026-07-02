import chromadb
client = chromadb.PersistentClient(path="./chroma_db")
col = client.get_or_create_collection("knowledge_base")

def index_docs(docs):  # docs: list[(id, text, metadata)]
    col.add(
        ids=[d[0] for d in docs],
        documents=[d[1] for d in docs],
        metadatas=[d[2] for d in docs],
    )

def search(query, k=4):
    res = col.query(query_texts=[query], n_results=k)
    return list(zip(res["ids"][0], res["documents"][0], res["metadatas"][0]))