"""
Debug-/Trace-Aufzeichnung + persistente Observability.

Drei Ebenen, gespeist aus DENSELBEN `log(kind, **data)`-Aufrufen (kein Call-Site-Umbau nötig):

  1. Ring-Puffer (im Admin-UI ein-/ausschaltbar)  — Live-Detailansicht, flüchtig.
  2. Persistente JSONL-Logs (immer an, append-only)  — wichtige Ereignisse überleben Neustarts,
     liegen unter logs/events-YYYYMMDD.jsonl, sodass „warum war er gestern verwirrt?" beantwortbar ist.
  3. Aggregierte Metriken (immer an, in-memory)  — Turn-Latenz, Tool-Calls, Fehler, 502/Swaps;
     Snapshot via metrics() bzw. /api/admin/metrics.

Bei deaktiviertem Ring-Puffer ist der Pfad weiterhin günstig: Metriken sind ein paar Integer-Inkremente,
JSONL nur für eine kleine Whitelist wichtiger Ereignisse.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

enabled = False
_events: deque = deque(maxlen=1000)
_lock = threading.Lock()
_seq = 0

# ── Persistente JSONL-Logs ───────────────────────────────────────────────────
_LOG_DIR = Path(os.environ.get("JARVIS_LOG_DIR", Path(__file__).resolve().parent / "logs"))
# Nur wirklich aussagekräftige Ereignisse persistieren (kein Rauschen, kein PII-Volltext der Audios).
_PERSIST_KINDS = {"turn", "turn_done", "llm_error", "retry", "tool", "tool_loop_abort",
                  "automation_done", "llm_502", "context_trim", "unverified_claim",
                  "mcp_startup", "mcp_refresh", "mcp_refresh_error", "startup_error", "smarthome_hint"}

# Felder, die Nutzer-Inhalte enthalten — bei aktivem log_redact aus den JSONL-Logs entfernt
# (es bleibt nur die Länge erhalten, damit Diagnose ohne Klartext möglich ist).
_REDACT_FIELDS = ("message", "reply", "args", "result")
_last_cleanup_day = ""


def _cfg():
    try:
        import config
        return config.get()
    except Exception:
        return {}


def _cleanup_old_logs(cfg: dict) -> None:
    """JSONL-Logs älter als log_retention_days löschen (höchstens 1× pro Tag)."""
    global _last_cleanup_day
    today = time.strftime("%Y%m%d")
    if _last_cleanup_day == today:
        return
    _last_cleanup_day = today
    try:
        days = int(cfg.get("log_retention_days", 14))
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        for path in _LOG_DIR.glob("events-*.jsonl"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
    except Exception:
        pass


def _redact(rec: dict, cfg: dict) -> dict:
    """Bei log_redact: Nutzer-Inhalte durch Längenmarker ersetzen (Diagnose bleibt, PII raus)."""
    if not cfg.get("log_redact", False):
        return rec
    out = dict(rec)
    for f in _REDACT_FIELDS:
        if f in out and out[f] is not None:
            out[f] = f"<redacted {len(str(out[f]))} chars>"
    return out

# ── Aggregierte Metriken ─────────────────────────────────────────────────────
_metrics: dict = {
    "started_at": time.time(),
    "turns": 0,
    "turns_error": 0,
    "tool_calls": 0,
    "tool_calls_by_name": defaultdict(int),
    "llm_errors": 0,
    "llm_502": 0,
    "retries": 0,
    "context_trims": 0,
    "unverified_claims": 0,
    "turn_ms_sum": 0,
    "turn_ms_count": 0,
    "turn_ms_max": 0,
}


def set_enabled(b: bool) -> None:
    global enabled
    enabled = bool(b)


def _update_metrics(kind: str, data: dict) -> None:
    m = _metrics
    if kind == "turn_done":
        m["turns"] += 1
        if data.get("error"):
            m["turns_error"] += 1
        ms = int(data.get("ms") or 0)
        if ms:
            m["turn_ms_sum"] += ms
            m["turn_ms_count"] += 1
            m["turn_ms_max"] = max(m["turn_ms_max"], ms)
    elif kind == "tool":
        m["tool_calls"] += 1
        m["tool_calls_by_name"][str(data.get("name"))] += 1
    elif kind == "llm_error":
        m["llm_errors"] += 1
        if "502" in str(data.get("error", "")):
            m["llm_502"] += 1
    elif kind == "llm_502":
        m["llm_502"] += 1
    elif kind == "retry":
        m["retries"] += 1
    elif kind == "context_trim":
        m["context_trims"] += 1
    elif kind == "unverified_claim":
        m["unverified_claims"] += 1


def _append_jsonl(kind: str, data: dict) -> None:
    try:
        cfg = _cfg()
        _cleanup_old_logs(cfg)
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        rec = _redact({"t": round(time.time(), 3), "kind": kind, **data}, cfg)
        path = _LOG_DIR / f"events-{time.strftime('%Y%m%d')}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass     # Observability darf den Betrieb nie stören


def log(kind: str, **data) -> None:
    """Zentrale Aufzeichnung. Speist Ring-Puffer (wenn an), Metriken (immer) und JSONL (wichtige Kinds)."""
    try:
        with _lock:
            _update_metrics(kind, data)
        if kind in _PERSIST_KINDS:
            _append_jsonl(kind, data)
    except Exception:
        pass
    if not enabled:
        return
    global _seq
    with _lock:
        _seq += 1
        _events.append({"id": _seq, "t": round(time.time(), 3), "kind": kind, **data})


def events() -> list[dict]:
    with _lock:
        return list(_events)


def metrics() -> dict:
    """Aggregierter Snapshot für /api/admin/metrics."""
    with _lock:
        m = _metrics
        cnt = m["turn_ms_count"] or 1
        up = max(1.0, time.time() - m["started_at"])
        return {
            "uptime_s": int(up),
            "turns": m["turns"],
            "turns_error": m["turns_error"],
            "turn_error_rate": round(m["turns_error"] / (m["turns"] or 1), 3),
            "turn_ms_avg": int(m["turn_ms_sum"] / cnt),
            "turn_ms_max": m["turn_ms_max"],
            "tool_calls": m["tool_calls"],
            "tool_calls_by_name": dict(sorted(m["tool_calls_by_name"].items(),
                                              key=lambda kv: -kv[1])[:20]),
            "llm_errors": m["llm_errors"],
            "llm_502": m["llm_502"],
            "retries": m["retries"],
            "context_trims": m["context_trims"],
            "unverified_claims": m["unverified_claims"],
        }


def recent_persisted(limit: int = 200) -> list[dict]:
    """Letzte persistierte Ereignisse aus den JSONL-Dateien (für Post-Mortem ohne Repro)."""
    out: list[dict] = []
    try:
        files = sorted(_LOG_DIR.glob("events-*.jsonl"))
        for path in reversed(files):
            lines = path.read_text(encoding="utf-8").splitlines()
            for line in reversed(lines):
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
                if len(out) >= limit:
                    return out
    except Exception:
        pass
    return out


def clear() -> None:
    with _lock:
        _events.clear()
