import os
import sys
from pathlib import Path
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from sentence_transformers import SentenceTransformer
import chromadb
from chromadb.utils import embedding_functions

# --- Konfiguration ---
REPO_BASE = "/opt/efro-agent/repos"
REPO_NAMES = ["efro", "efro-brain", "efro-widget", "efro-shopify"]
ALLOWED_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx"}  # Code-Dateien

# --- Embedding-Funktion (lokal) ---
class LocalEmbeddingFunction(embedding_functions.EmbeddingFunction):
    def __init__(self):
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
    def __call__(self, texts):
        return self.model.encode(texts).tolist()

# --- Chroma Client initialisieren ---
client = chromadb.PersistentClient(path="/opt/efro-agent/chroma_db")
embedding_fn = LocalEmbeddingFunction()

# Collection anlegen (falls nicht vorhanden, wird sie erstellt)
collection = client.get_or_create_collection(
    name="efro_code",
    embedding_function=embedding_fn
)

# --- Code-Splitter (sprachneutral) ---
text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=2000,
    chunk_overlap=200,
    separators=["\n\n", "\n", " ", ""],
    length_function=len,
)

print("Indexierung gestartet...")
doc_count = 0

for repo in REPO_NAMES:
    repo_path = Path(REPO_BASE) / repo
    if not repo_path.exists():
        print(f"⚠️ Repo {repo} nicht gefunden: {repo_path}")
        continue
    print(f"📁 Verarbeite {repo} ...")
    for file_path in repo_path.rglob("*"):
        if file_path.suffix in ALLOWED_EXTENSIONS:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception as e:
                print(f"   Fehler beim Lesen {file_path}: {e}")
                continue

            # Datei in Dokumente splitten
            docs = text_splitter.create_documents(
                [content],
                metadatas=[{"repo": repo, "path": str(file_path)}]
            )
            for i, doc in enumerate(docs):
                # Eindeutige ID: repo::Pfad::Chunknummer
                doc_id = f"{repo}::{file_path}::{i}"
                collection.add(
                    documents=[doc.page_content],
                    metadatas=[doc.metadata],
                    ids=[doc_id]
                )
                doc_count += 1
            # Optional: Fortschritt anzeigen
            if doc_count % 100 == 0:
                print(f"   {doc_count} Chunks indiziert...")

print(f"\n✅ Indexierung abgeschlossen! {doc_count} Chunks gespeichert.")
print(f"Collection-Name: efro_code")
print(f"Speicherort: /opt/efro-agent/chroma_db")
