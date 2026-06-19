"""
Automatisierungen — autonome, geplante & ereignisgesteuerte Selbstläufe des Agenten.

JARVIS kann hierüber unabhängig vom Nutzer handeln: zeitgesteuert (einmalig / Intervall /
täglich / wöchentlich) oder durch Ereignisse (z.B. ein erkannter Sprecher, ein MCP-/Smarthome-
Event). Bei Fälligkeit ruft der Manager den injizierten `runner` (= Agenten-Tool-Loop) mit der
hinterlegten Aufgabe auf und liefert dessen Ergebnis über `deliver` (= universeller Rückkanal
`announce`) an die Zielquelle. Antwortet der Agent mit dem Token SILENT, wird nichts gemeldet
(→ „nur bei Bedarf benachrichtigen").

Persistenz: automations.json (überlebt Neustarts). Bewusst DB-frei, damit der Scheduler auch
bei pgvector-Ausfall läuft.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional

_PATH = Path(__file__).resolve().parent / "automations.json"
SILENT_TOKEN = "SILENT"
WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]

# Register aller serverseitig auslösbaren Ereignisse (für Ereignis-Trigger).
# name -> {"label": deutsch, "fields": [Payload-Felder, gegen die "match" prüft]}
KNOWN_EVENTS: dict[str, dict] = {
    "speaker_recognized":  {"label": "Sprecher erkannt",          "fields": ["username", "user_id"]},
    "device_connected":    {"label": "Gerät verbunden",           "fields": ["type", "name", "room"]},
    "device_disconnected": {"label": "Gerät getrennt",            "fields": ["type", "name", "room"]},
    "satellite_listening": {"label": "Satellit hört zu (Wake-Word)", "fields": ["name", "room"]},
    "timer_elapsed":       {"label": "Timer/Wecker abgelaufen",   "fields": ["label"]},
    "user_created":        {"label": "Neuer Nutzer angelegt",     "fields": ["username"]},
    "voice_enrolled":      {"label": "Stimmprofil ergänzt",       "fields": ["username"]},
    "document_uploaded":   {"label": "Dokument hochgeladen",      "fields": ["source", "namespace"]},
    "memory_saved":        {"label": "Fakt im Gedächtnis gespeichert", "fields": ["namespace"]},
    "mcp_event":           {"label": "Externes MCP-/Smarthome-Ereignis", "fields": ["source", "detail"]},
}


def known_events() -> list[dict]:
    return [{"name": k, "label": v["label"], "fields": v["fields"]} for k, v in KNOWN_EVENTS.items()]


def _now() -> float:
    return time.time()


def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = (s or "0:0").split(":")[:2]
    return max(0, min(23, int(h))), max(0, min(59, int(m)))


def compute_next(trigger: dict, after: float | None = None) -> float | None:
    """Nächste Fälligkeit (epoch) für Zeit-Trigger; None für Ereignis-/abgelaufene once-Trigger."""
    after = after or _now()
    t = trigger.get("type")
    if t == "once":
        at = float(trigger.get("at", 0))
        return at if at > after else None
    if t == "interval":
        sec = max(10, int(trigger.get("seconds", 3600)))
        return after + sec
    if t in ("daily", "weekly"):
        h, m = _parse_hhmm(trigger.get("time", "08:00"))
        base = datetime.fromtimestamp(after)
        cand = base.replace(hour=h, minute=m, second=0, microsecond=0)
        if t == "daily":
            if cand.timestamp() <= after:
                cand += timedelta(days=1)
            return cand.timestamp()
        days = trigger.get("weekdays") or list(range(7))      # 0=Mo … 6=So
        for add in range(0, 8):
            c = cand + timedelta(days=add)
            if c.weekday() in days and c.timestamp() > after:
                return c.timestamp()
        return None
    return None  # event / unbekannt


def trigger_summary(trigger: dict) -> str:
    """Menschen-/sprechbare Kurzbeschreibung des Auslösers (Deutsch)."""
    t = trigger.get("type")
    if t == "once":
        return "einmalig am " + datetime.fromtimestamp(float(trigger.get("at", 0))).strftime("%d.%m.%Y %H:%M")
    if t == "interval":
        sec = int(trigger.get("seconds", 3600))
        return f"alle {sec // 60} Min" if sec < 3600 else f"alle {sec // 3600} Std"
    if t == "daily":
        return f"täglich um {trigger.get('time', '08:00')}"
    if t == "weekly":
        ds = ", ".join(WEEKDAYS_DE[d] for d in (trigger.get("weekdays") or []))
        return f"{ds or 'wöchentlich'} um {trigger.get('time', '08:00')}"
    if t == "event":
        m = trigger.get("match")
        return f"bei Ereignis „{trigger.get('event', '?')}“" + (f" ({m})" if m else "")
    return "—"


class AutomationManager:
    def __init__(self) -> None:
        self._items: dict[str, dict] = {}
        self._seq = 0
        self._running: set[str] = set()          # ids, die gerade laufen (Doppellauf-Schutz)
        self._last_event_run: dict[str, float] = {}
        self.runner: Optional[Callable[[dict, dict | None], Awaitable[str]]] = None
        self.deliver: Optional[Callable[[dict, str, dict | None], Awaitable[None]]] = None
        self.cooldown_s = 30
        self.enabled = True
        self._task: Optional[asyncio.Task] = None
        self._load()

    # ── Persistenz ─────────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            data = json.loads(_PATH.read_text(encoding="utf-8"))
            self._items = {a["id"]: a for a in data.get("items", [])}
            self._seq = data.get("seq", 0)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[automations] Laden fehlgeschlagen: {e}")

    def _save(self) -> None:
        try:
            _PATH.write_text(json.dumps({"seq": self._seq, "items": list(self._items.values())},
                                        indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"[automations] Speichern fehlgeschlagen: {e}")

    # ── CRUD ───────────────────────────────────────────────────────────────────
    def create(self, title: str, task: str, trigger: dict, *,
               owner_user_id: int | None = None, target_session: str | None = None) -> dict:
        self._seq += 1
        aid = f"a{self._seq}"
        a = {
            "id": aid,
            "title": title or f"Automatisierung {aid}",
            "task": task,
            "trigger": trigger,
            "owner_user_id": owner_user_id,
            "target_session": target_session,
            "enabled": True,
            "created_at": _now(),
            "last_run": None,
            "last_result": None,
            "run_count": 0,
            "next_run": compute_next(trigger),
        }
        self._items[aid] = a
        self._save()
        return a

    def update(self, aid: str, **fields) -> dict | None:
        a = self._items.get(aid)
        if not a:
            return None
        for k in ("title", "task", "enabled", "trigger", "target_session", "owner_user_id"):
            if k in fields and fields[k] is not None:
                a[k] = fields[k]
        a["next_run"] = compute_next(a["trigger"]) if a["enabled"] else None
        self._save()
        return a

    def delete(self, aid: str) -> bool:
        ok = self._items.pop(aid, None) is not None
        if ok:
            self._save()
        return ok

    def get(self, aid: str) -> dict | None:
        return self._items.get(aid)

    def list(self, owner_user_id: int | None = "__all__") -> list[dict]:
        items = list(self._items.values())
        if owner_user_id != "__all__":
            items = [a for a in items if a.get("owner_user_id") == owner_user_id]
        return sorted(items, key=lambda a: (a.get("next_run") or 9e18, a["created_at"]))

    def find(self, owner_user_id: int | None, identifier: str) -> dict | None:
        ident = (identifier or "").strip().lower()
        for a in self._items.values():
            if a.get("owner_user_id") == owner_user_id and (a["id"] == identifier or a["title"].lower() == ident):
                return a
        return None

    # ── Ausführung ──────────────────────────────────────────────────────────────
    async def run_now(self, aid: str, payload: dict | None = None) -> dict:
        a = self._items.get(aid)
        if not a:
            return {"ok": False, "error": "unbekannt"}
        await self._run_one(a, payload, manual=True)
        return {"ok": True, "last_result": a.get("last_result")}

    async def _run_one(self, a: dict, payload: dict | None, manual: bool = False) -> None:
        if a["id"] in self._running or not self.runner:
            return
        self._running.add(a["id"])
        try:
            text = await self.runner(a, payload)
        except Exception as e:
            text = f"[Fehler] {e}"
            print(f"[automations] Lauf {a['id']} fehlgeschlagen: {e}")
        finally:
            self._running.discard(a["id"])
        a["last_run"] = _now()
        a["run_count"] = a.get("run_count", 0) + 1
        a["last_result"] = (text or "")[:500]
        # Zeit-Trigger neu planen; einmalige deaktivieren
        if a["trigger"].get("type") not in ("event",):
            nxt = compute_next(a["trigger"])
            a["next_run"] = nxt
            if nxt is None:
                a["enabled"] = False
        self._save()
        # Ausliefern, außer der Agent meldete SILENT (= nichts Berichtenswertes)
        clean = (text or "").strip()
        if clean and clean.upper().strip(".!") != SILENT_TOKEN and self.deliver:
            try:
                await self.deliver(a, clean, payload)
            except Exception as e:
                print(f"[automations] Auslieferung {a['id']} fehlgeschlagen: {e}")

    # ── Ereignis-Trigger ─────────────────────────────────────────────────────────
    def _event_match(self, trigger: dict, payload: dict) -> bool:
        m = (trigger.get("match") or "").strip().lower()
        if not m:
            return True
        hay = " ".join(str(v) for v in (payload or {}).values()).lower()
        return m in hay

    async def dispatch_event(self, event: str, payload: dict | None = None) -> int:
        """Ereignis melden → passende Automatisierungen (mit Cooldown) auslösen. Gibt Anzahl zurück."""
        if not self.enabled:
            return 0
        payload = payload or {}
        fired = 0
        for a in list(self._items.values()):
            tr = a.get("trigger", {})
            if not a.get("enabled") or tr.get("type") != "event" or tr.get("event") != event:
                continue
            if not self._event_match(tr, payload):
                continue
            last = self._last_event_run.get(a["id"], 0)
            if _now() - last < self.cooldown_s:
                continue
            self._last_event_run[a["id"]] = _now()
            asyncio.create_task(self._run_one(a, payload))
            fired += 1
        return fired

    # ── Scheduler-Loop ────────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            if not self.enabled:
                continue
            now = _now()
            for a in list(self._items.values()):
                if not a.get("enabled"):
                    continue
                nr = a.get("next_run")
                if nr and nr <= now and a["id"] not in self._running:
                    asyncio.create_task(self._run_one(a, None))

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())


manager = AutomationManager()


def emit(event: str, payload: dict | None = None) -> None:
    """Ereignis feuern (fire-and-forget) — sicher aus async-Kontexten aufrufbar.
    Löst alle passenden Ereignis-Automatisierungen aus (mit Cooldown)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return                                    # kein laufender Loop → ignorieren
    loop.create_task(manager.dispatch_event(event, payload or {}))
