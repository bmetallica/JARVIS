"""
Sprach-Biometrie — Sprecher-Erkennung (wer spricht?).

  • Enrollment: je Nutzer mehrere kurze Audioschnipsel → Sprecher-Embedding
    (resemblyzer, 256-dim) → in pgvector-Tabelle `voiceprints` (ref. user_id).
  • Identify:   Audio einer Äußerung → Embedding → 1:N-Vergleich (Cosine) gegen
    alle Voiceprints → bester Treffer über Schwellwert ⇒ {user_id, username, score},
    sonst None (Gast/unbekannt).

Läuft auf CPU. Decodierung beliebiger Browser-Formate (webm/opus) via ffmpeg/librosa.
"""
from __future__ import annotations

import os
import tempfile
import threading

import store

_encoder = None
_enc_lock = threading.Lock()
_vp_init = False


def _voice_encoder():
    global _encoder
    with _enc_lock:
        if _encoder is None:
            from resemblyzer import VoiceEncoder
            _encoder = VoiceEncoder()       # lädt das Modell einmalig (CPU)
        return _encoder


def init() -> None:
    global _vp_init
    if _vp_init:
        return
    store.init()
    with store._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS voiceprints (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
            embedding vector(256),
            created_at TIMESTAMPTZ DEFAULT now());""")
        c.execute("CREATE INDEX IF NOT EXISTS voiceprints_emb_idx ON voiceprints "
                  "USING hnsw (embedding vector_cosine_ops);")
    _vp_init = True


def embed_audio(audio_bytes: bytes, filename: str = "audio.webm") -> list[float]:
    """Audio (beliebiges Format) → 256-dim Sprecher-Embedding."""
    from resemblyzer import preprocess_wav
    suffix = os.path.splitext(filename)[1] or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes)
        path = f.name
    try:
        wav = preprocess_wav(path)          # lädt via librosa/ffmpeg, 16 kHz, VAD-getrimmt
        emb = _voice_encoder().embed_utterance(wav)
        return [float(x) for x in emb]
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def add_voiceprint(user_id: int, embedding: list[float]) -> None:
    """Ein bereits berechnetes Embedding als Voiceprint speichern (z.B. das gepufferte
    der letzten, nicht erkannten Äußerung beim Onboarding)."""
    init()
    with store._conn() as c:
        c.execute("INSERT INTO voiceprints (user_id, embedding) VALUES (%s, %s::vector);",
                  (user_id, store._vec_literal(embedding)))


def enroll(user_id: int, audio_bytes: bytes, filename: str = "audio.webm") -> None:
    add_voiceprint(user_id, embed_audio(audio_bytes, filename))


def count_for_user(user_id: int) -> int:
    init()
    with store._conn() as c:
        return c.execute("SELECT COUNT(*) FROM voiceprints WHERE user_id=%s;", (user_id,)).fetchone()[0]


def clear_user(user_id: int) -> int:
    init()
    with store._conn() as c:
        return c.execute("DELETE FROM voiceprints WHERE user_id=%s;", (user_id,)).rowcount


def identify_by_embedding(emb: list[float], threshold: float = 0.75) -> dict | None:
    """Bester Sprecher-Treffer über Schwellwert für ein bereits berechnetes Embedding."""
    init()
    with store._conn() as c:
        row = c.execute(
            "SELECT v.user_id, u.username, 1 - (v.embedding <=> %s::vector) AS score "
            "FROM voiceprints v JOIN users u ON u.id = v.user_id "
            "ORDER BY v.embedding <=> %s::vector LIMIT 1;",
            (store._vec_literal(emb), store._vec_literal(emb)),
        ).fetchone()
    if not row or row[2] is None or float(row[2]) < threshold:
        return None
    return {"user_id": row[0], "username": row[1], "confidence": round(float(row[2]), 3)}


def identify(audio_bytes: bytes, filename: str, threshold: float = 0.75) -> dict | None:
    """Bester Sprecher-Treffer über Schwellwert, sonst None (Gast)."""
    return identify_by_embedding(embed_audio(audio_bytes, filename), threshold)
