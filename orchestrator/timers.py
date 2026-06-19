"""
Timer-Manager — mehrere parallele Timer, jeder an seine Ursprungs-Session gebunden.

Läuft auf dem asyncio-Loop des Orchestrators (FastAPI). `add()` wird aus dem
async Chat-Handler (auf dem Loop) aufgerufen, daher reicht `call_later` ohne
Thread-Bridging. Beim Ablauf ruft der Manager den `on_fire`-Callback, der die
Ausgabe an die richtige Session pusht.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional


def format_duration(seconds: int) -> str:
    """Sekunden → deutsche, sprechbare Dauer (z.B. '1 Stunde 5 Minuten')."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h} Stunde" + ("n" if h != 1 else ""))
    if m:
        parts.append(f"{m} Minute" + ("n" if m != 1 else ""))
    if s and not h:                      # Sekunden nur zeigen, wenn keine Stunden
        parts.append(f"{s} Sekunde" + ("n" if s != 1 else ""))
    return " ".join(parts) or "0 Sekunden"


class TimerManager:
    def __init__(self) -> None:
        self._timers: dict = {}          # id -> {id, sid, label, fire_at, handle}
        self._seq = 0
        self.on_fire: Optional[Callable[[dict], Awaitable[None]]] = None

    def add(self, session_id: str, duration_seconds: int, label: str = "") -> dict:
        self._seq += 1
        tid = str(self._seq)
        label = label or f"Timer {tid}"
        loop = asyncio.get_running_loop()
        info = {
            "id": tid,
            "session_id": session_id,
            "label": label,
            "duration": int(duration_seconds),
            "fire_at": time.time() + int(duration_seconds),
        }
        info["handle"] = loop.call_later(max(0, int(duration_seconds)), self._fire, tid)
        self._timers[tid] = info
        return info

    def _fire(self, tid: str) -> None:
        info = self._timers.pop(tid, None)
        if info and self.on_fire:
            asyncio.create_task(self.on_fire(info))

    def list(self, session_id: str | None = None) -> list[dict]:
        now = time.time()
        out = []
        for t in self._timers.values():
            if session_id is None or t["session_id"] == session_id:
                out.append({
                    "id": t["id"],
                    "label": t["label"],
                    "remaining": max(0, int(t["fire_at"] - now)),
                })
        return out

    def cancel(self, session_id: str, ident: str) -> dict | None:
        """Timer per ID oder Label (innerhalb der Session) abbrechen."""
        for tid, t in list(self._timers.items()):
            if t["session_id"] == session_id and (t["id"] == ident or t["label"].lower() == ident.lower()):
                h = t.get("handle")
                if h:
                    h.cancel()
                return self._timers.pop(tid, None)
        return None


manager = TimerManager()
