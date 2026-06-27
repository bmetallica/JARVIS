"""
Plugin-Event-Bus — schlankes In-Process Pub/Sub für den Plugin-WebSocket (/api/v1/ws).

  • Plugins abonnieren Topics (z.B. "jarvis/plugin/adhs/#", "jarvis/core/timer_elapsed").
  • Plugins publizieren eigene Events ("jarvis/plugin/adhs/gamification/xp").
  • Core-Events aus automations.emit() werden als "jarvis/core/<event>" eingespeist
    (forward_core_event als Listener registriert, siehe app._startup).

Topic-Matching: exakt ODER Präfix mit '#'-Wildcard am Ende
("jarvis/plugin/adhs/#" matcht alles unter jarvis/plugin/adhs/...).
Reine Speicher-Lösung (ein Orchestrator-Prozess) — kein externer Broker nötig.
"""
from __future__ import annotations

import asyncio

# subscriber_id -> {"queue": asyncio.Queue, "topics": set[str], "plugin": str}
_subs: dict[int, dict] = {}
_next_id = 0


def _matches(pattern: str, topic: str) -> bool:
    if pattern == topic or pattern == "#" or pattern == "*":
        return True
    if pattern.endswith("/#"):
        return topic.startswith(pattern[:-1])      # "a/b/#" → Präfix "a/b/"
    if pattern.endswith("#"):
        return topic.startswith(pattern[:-1])
    return False


def subscribe(topics, plugin: str = "") -> tuple[int, asyncio.Queue]:
    """Neuen Subscriber registrieren. Gibt (sub_id, queue) zurück."""
    global _next_id
    _next_id += 1
    sid = _next_id
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    _subs[sid] = {"queue": q, "topics": set(topics or []), "plugin": plugin}
    return sid, q


def set_topics(sub_id: int, topics) -> None:
    if sub_id in _subs:
        _subs[sub_id]["topics"] = set(topics or [])


def add_topics(sub_id: int, topics) -> None:
    if sub_id in _subs:
        _subs[sub_id]["topics"].update(topics or [])


def unsubscribe(sub_id: int) -> None:
    _subs.pop(sub_id, None)


async def publish(topic: str, payload: dict | None = None, *, source: str = "") -> int:
    """Event an alle passenden Subscriber zustellen. Gibt die Empfängerzahl zurück."""
    event = {"op": "event", "topic": topic, "payload": payload or {}, "source": source}
    n = 0
    for sub in list(_subs.values()):
        if any(_matches(p, topic) for p in sub["topics"]):
            try:
                sub["queue"].put_nowait(event)
                n += 1
            except asyncio.QueueFull:
                pass          # langsamer Client → Event verwerfen (kein Backpressure auf den Core)
    return n


def forward_core_event(event: str, payload: dict | None = None) -> None:
    """Listener für automations.emit — spiegelt Core-Events als jarvis/core/<event> in den Bus.
    Fire-and-forget; sicher aus async-Kontexten."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(publish(f"jarvis/core/{event}", payload or {}, source="core"))


def stats() -> dict:
    return {"subscribers": len(_subs),
            "topics": sorted({t for s in _subs.values() for t in s["topics"]})}
