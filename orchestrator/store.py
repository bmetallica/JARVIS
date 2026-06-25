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


# ── Kurzzeitgedächtnis: persistenter Gesprächsverlauf ────────────────────────
# Überlebt Neustarts (vorher nur im RAM von session_hub). Pro session_id, mit user_key
# zur Trennung verschiedener Sprecher.
_history_initialised = False


def _history_init() -> None:
    global _history_initialised
    if _history_initialised:
        return
    init()
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id         BIGSERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_key   TEXT,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now()
            );
        """)
        c.execute("CREATE INDEX IF NOT EXISTS chat_history_sid_idx ON chat_history (session_id, id);")
    _history_initialised = True


def history_append(session_id: str, role: str, content: str, user_key: str | None = None) -> None:
    _history_init()
    with _conn() as c:
        c.execute(
            "INSERT INTO chat_history (session_id, user_key, role, content) VALUES (%s, %s, %s, %s);",
            (session_id, user_key, role, content),
        )


def history_load(session_id: str, limit: int = 20) -> list[dict]:
    """Letzte `limit` Nachrichten dieser Session in chronologischer Reihenfolge."""
    _history_init()
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM chat_history WHERE session_id=%s ORDER BY id DESC LIMIT %s;",
            (session_id, limit),
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def history_reset(session_id: str) -> None:
    _history_init()
    with _conn() as c:
        c.execute("DELETE FROM chat_history WHERE session_id=%s;", (session_id,))


# ── Agent-kuratiertes Nutzermodell (Phase 3) ─────────────────────────────────
_profile_initialised = False


def _profile_init() -> None:
    global _profile_initialised
    if _profile_initialised:
        return
    init()
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                user_id    BIGINT PRIMARY KEY,
                content    TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ DEFAULT now()
            );
        """)
    _profile_initialised = True


def profile_get(user_id: int) -> str:
    if user_id is None:
        return ""
    _profile_init()
    with _conn() as c:
        row = c.execute("SELECT content FROM user_profile WHERE user_id=%s;", (user_id,)).fetchone()
    return row[0] if row else ""


def profile_set(user_id: int, content: str) -> None:
    if user_id is None:
        return
    _profile_init()
    with _conn() as c:
        c.execute(
            "INSERT INTO user_profile (user_id, content, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (user_id) DO UPDATE SET content=EXCLUDED.content, updated_at=now();",
            (user_id, content),
        )


def profile_all() -> list[dict]:
    _profile_init()
    with _conn() as c:
        rows = c.execute("SELECT user_id, content, updated_at FROM user_profile ORDER BY user_id;").fetchall()
    return [{"user_id": r[0], "content": r[1], "updated_at": str(r[2])} for r in rows]


def profile_age_seconds(user_id: int):
    """Sekunden seit der letzten Profil-Aktualisierung, oder None wenn noch keins existiert."""
    _profile_init()
    with _conn() as c:
        row = c.execute("SELECT EXTRACT(EPOCH FROM (now() - updated_at)) FROM user_profile WHERE user_id=%s;",
                        (user_id,)).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def history_for_user(user_id: int, limit: int = 16) -> list[dict]:
    """Letzte Nachrichten eines Nutzers (über alle Sessions) — für Profil-Generierung."""
    _history_init()
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM chat_history WHERE user_key=%s ORDER BY id DESC LIMIT %s;",
            (str(user_id), limit)).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def history_trim(session_id: str, keep: int = 200) -> None:
    """Alte Zeilen kappen, damit die Tabelle nicht unbegrenzt wächst (behält die neuesten `keep`)."""
    _history_init()
    with _conn() as c:
        c.execute(
            "DELETE FROM chat_history WHERE session_id=%s AND id NOT IN "
            "(SELECT id FROM chat_history WHERE session_id=%s ORDER BY id DESC LIMIT %s);",
            (session_id, session_id, keep),
        )


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
