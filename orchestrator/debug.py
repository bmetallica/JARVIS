"""
Debug-/Trace-Aufzeichnung — im Admin-UI ein-/ausschaltbar.

Wenn aktiv, werden alle Pipeline-Vorgänge (STT, Turn, LLM-Aufrufe, Tool-/MCP-Aufrufe
mit Argumenten/Ergebnis/Dauer, Retries, Fehler) in einen Ring-Puffer geschrieben und
im Admin-UI anzeigbar. Bei deaktiviertem Debug ist `log()` praktisch kostenlos.
"""
from __future__ import annotations

import threading
import time
from collections import deque

enabled = False
_events: deque = deque(maxlen=1000)
_lock = threading.Lock()
_seq = 0


def set_enabled(b: bool) -> None:
    global enabled
    enabled = bool(b)


def log(kind: str, **data) -> None:
    if not enabled:
        return
    global _seq
    with _lock:
        _seq += 1
        _events.append({"id": _seq, "t": round(time.time(), 3), "kind": kind, **data})


def events() -> list[dict]:
    with _lock:
        return list(_events)


def clear() -> None:
    with _lock:
        _events.clear()
