"""
Vektor-Store (PostgreSQL + pgvector) — gemeinsame Basis für
  • Langzeitgedächtnis   (kind='memory')
  • RAG-Wissensbasis      (kind='document')

Eine Tabelle, getrennt nach `kind` und `namespace` (Namespace = später pro Nutzer,
vorerst 'default'). Embeddings kommen aus nomic-embed-text (768-dim).
"""
from __future__ import annotations

import os
import threading

import psycopg

DSN = os.environ.get(
    "JARVIS_PG_DSN",
    "host=localhost port=5440 dbname=jarvis user=jarvis password=jarvis",
)
DIM = 768

_init_lock = threading.Lock()
_initialised = False


def _conn():
    return psycopg.connect(DSN, autocommit=True)


def init() -> None:
    """Extension, Tabelle und Indizes anlegen (idempotent)."""
    global _initialised
    with _init_lock:
        if _initialised:
            return
        with _conn() as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            c.execute(f"""
                CREATE TABLE IF NOT EXISTS vectors (
                    id         BIGSERIAL PRIMARY KEY,
                    namespace  TEXT NOT NULL DEFAULT 'default',
                    kind       TEXT NOT NULL,
                    source     TEXT,
                    content    TEXT NOT NULL,
                    embedding  vector({DIM}),
                    created_at TIMESTAMPTZ DEFAULT now()
                );
            """)
            c.execute("CREATE INDEX IF NOT EXISTS vectors_emb_idx ON vectors "
                      "USING hnsw (embedding vector_cosine_ops);")
            c.execute("CREATE INDEX IF NOT EXISTS vectors_ns_kind_idx ON vectors (namespace, kind);")
        _initialised = True


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def add(kind: str, content: str, embedding: list[float],
        namespace: str = "default", source: str = "") -> int:
    init()
    with _conn() as c:
        row = c.execute(
            "INSERT INTO vectors (namespace, kind, source, content, embedding) "
            "VALUES (%s, %s, %s, %s, %s::vector) RETURNING id;",
            (namespace, kind, source, content, _vec_literal(embedding)),
        ).fetchone()
        return row[0]


def add_many(kind: str, items: list[tuple[str, list[float]]],
             namespace: str = "default", source: str = "") -> int:
    """items: Liste von (content, embedding). Gibt Anzahl eingefügter Zeilen zurück."""
    init()
    with _conn() as c:
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO vectors (namespace, kind, source, content, embedding) "
                "VALUES (%s, %s, %s, %s, %s::vector);",
                [(namespace, kind, source, content, _vec_literal(emb)) for content, emb in items],
            )
    return len(items)


def search(kind: str, embedding: list[float], namespace: str = "default",
           k: int = 5, min_score: float = 0.0) -> list[dict]:
    """Semantische Suche (Cosine). Gibt [{content, source, score}] zurück (score 0..1)."""
    init()
    with _conn() as c:
        rows = c.execute(
            "SELECT content, source, 1 - (embedding <=> %s::vector) AS score "
            "FROM vectors WHERE namespace = %s AND kind = %s "
            "ORDER BY embedding <=> %s::vector LIMIT %s;",
            (_vec_literal(embedding), namespace, kind, _vec_literal(embedding), k),
        ).fetchall()
    return [{"content": r[0], "source": r[1], "score": float(r[2])}
            for r in rows if r[2] is not None and float(r[2]) >= min_score]


def list_sources(kind: str = "document", namespace: str = "default") -> list[dict]:
    init()
    with _conn() as c:
        rows = c.execute(
            "SELECT source, COUNT(*) FROM vectors WHERE namespace=%s AND kind=%s "
            "GROUP BY source ORDER BY source;",
            (namespace, kind),
        ).fetchall()
    return [{"source": r[0], "chunks": r[1]} for r in rows]


def delete_source(source: str, namespace: str = "default") -> int:
    init()
    with _conn() as c:
        cur = c.execute("DELETE FROM vectors WHERE namespace=%s AND source=%s;", (namespace, source))
        return cur.rowcount
