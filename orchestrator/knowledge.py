"""
Gedächtnis + RAG-Wissensbasis — beide auf demselben pgvector-Store (store.py),
getrennt nach `kind`:
  • 'memory'   — Fakten über den Nutzer (Langzeitgedächtnis, auto-recall pro Turn)
  • 'document' — hochgeladene Dokumente (Chunks, per knowledge_search abrufbar)

Embeddings via services.embed (nomic-embed-text).
"""
from __future__ import annotations

import re

import services
import store


def _chunk(text: str, size: int = 800, overlap: int = 120) -> list[str]:
    """Text in überlappende Chunks zerlegen (Absatz-bewusst)."""
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) <= size:
        return [text] if text else []
    chunks, i = [], 0
    while i < len(text):
        end = min(i + size, len(text))
        # an Satz-/Absatzgrenze schneiden, wenn möglich
        cut = text.rfind("\n", i + size // 2, end)
        if cut == -1:
            cut = text.rfind(". ", i + size // 2, end)
        if cut != -1 and cut > i:
            end = cut + 1
        chunks.append(text[i:end].strip())
        i = max(end - overlap, end) if end < len(text) else end
    return [c for c in chunks if c]


# ── Gedächtnis ────────────────────────────────────────────────────────────────

def save_memory(cfg: dict, text: str, namespace: str = "default", category: str = "fact") -> None:
    emb = services.embed([text], cfg, task="search_document")[0]
    store.add("memory", text, emb, namespace=namespace, source=category)


def recall_memory(cfg: dict, query: str, namespace: str = "default",
                  k: int = 5, min_score: float = 0.5) -> list[dict]:
    emb = services.embed([query], cfg, task="search_query")[0]
    return store.search("memory", emb, namespace=namespace, k=k, min_score=min_score)


# ── Wissensbasis (Dokumente) ───────────────────────────────────────────────────

def ingest_document(cfg: dict, source: str, text: str, namespace: str = "default") -> int:
    chunks = _chunk(text)
    if not chunks:
        return 0
    embs = services.embed(chunks, cfg, task="search_document")
    store.add_many("document", list(zip(chunks, embs)), namespace=namespace, source=source)
    return len(chunks)


def search_knowledge(cfg: dict, query: str, namespace: str = "default",
                     k: int = 5, min_score: float = 0.4) -> list[dict]:
    emb = services.embed([query], cfg, task="search_query")[0]
    return store.search("document", emb, namespace=namespace, k=k, min_score=min_score)
