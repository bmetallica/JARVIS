"""
Session-Hub — quellen-bezogenes I/O-Routing.

Jede Ein-/Ausgabequelle (Browser-Tab, später ESP32-Satellit) öffnet eine
WebSocket-Verbindung und registriert sich mit einer Session-ID. Der Server kann
darüber **asynchrone Ausgaben gezielt an die Ursprungsquelle** zurückschicken —
z.B. einen Timer-Alarm exakt dorthin, wo der Timer erstellt wurde.

Bewusst einfach gehalten (In-Memory). Später: persistente Client-Registry (Phase 2).
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid


class SessionHub:
    def __init__(self) -> None:
        self._ws: dict = {}          # session_id -> WebSocket
        self._meta: dict = {}        # session_id -> {type, name, render, capabilities}
        self._calls: dict = {}       # request_id -> Future (Client-Aktion → Ergebnis)
        self._pending: dict = {}     # session_id -> [events]  (für offline/Reconnect)
        self._identity: dict = {}    # session_id -> {user_id, username, confidence}  (vom Sprecher-Erkenner)
        self._history: dict = {}     # session_id -> [{role, content}]  (server-seitiger Verlauf)
        self._last_uid: dict = {}    # session_id -> zuletzt aktiver user_id (für Sprecherwechsel)
        self._last_voice: dict = {}  # session_id -> letztes Stimm-Embedding (fürs Onboarding)
        self._onboard: dict = {}     # session_id -> None | "asked" | "skipped"
        self._seen: dict = {}        # session_id -> {first, last, **telemetry}  (für Geräteliste)
        self._dev: dict = {}         # session_id -> ts (Dev-/Bau-Modus aktiv → Denken erzwingen)
        self._loaded_hist: set = set()   # session_ids, deren Verlauf schon aus der DB geladen wurde
        self._last_tools: dict = {}  # session_id -> [{name, result}]  (letzte Tool-Ergebnisse für Rückfragen)
        self._summary: dict = {}     # session_id -> rollierende Zusammenfassung getrimmter alter Turns
        self._cancel: dict = {}      # session_id -> True, wenn laufender Turn abgebrochen werden soll (Barge-in)
        self._authed: dict = {}      # session_id -> identity (explizite Passwort-Anmeldung, Vorrang vor Stimme)

    def mark_dev(self, session_id: str | None) -> None:
        """Session ist in einem Entwicklungs-/Bau-Flow (Skill/Code/Browser-Automation)."""
        if session_id:
            self._dev[session_id] = time.time()

    def is_dev(self, session_id: str | None, ttl: float = 900) -> bool:
        return bool(session_id) and (time.time() - self._dev.get(session_id, 0) < ttl)

    def set_last_voice(self, session_id: str, embedding) -> None:
        self._last_voice[session_id] = embedding

    def get_last_voice(self, session_id: str | None):
        return self._last_voice.get(session_id) if session_id else None

    def onboarding_state(self, session_id: str):
        return self._onboard.get(session_id)

    def set_onboarding_state(self, session_id: str, state) -> None:
        if state is None:
            self._onboard.pop(session_id, None)
        else:
            self._onboard[session_id] = state

    # ── Explizite Anmeldung (Passwort) — hat Vorrang vor der Stimm-Identität ──
    def set_authed(self, session_id: str, identity: dict) -> None:
        if session_id and identity:
            self._authed[session_id] = identity

    def clear_authed(self, session_id: str) -> None:
        self._authed.pop(session_id, None)

    def is_authed(self, session_id: str | None) -> bool:
        return bool(session_id) and session_id in self._authed

    def set_identity(self, session_id: str, identity: dict | None) -> None:
        if identity:
            self._identity[session_id] = identity
        else:
            self._identity.pop(session_id, None)

    def get_identity(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        return self._authed.get(session_id) or self._identity.get(session_id)

    # ── Verlauf pro Session (vom Server geführt, NICHT vom Client) ──────────────
    def history(self, session_id: str, limit: int = 20) -> list[dict]:
        # Lazy-Load aus der DB beim ersten Zugriff nach (Neu-)Start → Verlauf überlebt Neustarts.
        if session_id not in self._loaded_hist:
            self._loaded_hist.add(session_id)
            if session_id not in self._history:
                try:
                    import store
                    loaded = store.history_load(session_id, limit)
                    if loaded:
                        self._history[session_id] = loaded
                except Exception:
                    pass
        return self._history.get(session_id, [])

    def append_history(self, session_id: str, role: str, content: str, limit: int = 20) -> None:
        h = self._history.setdefault(session_id, [])
        h.append({"role": role, "content": content})
        if len(h) > limit:
            del h[:-limit]
        # Write-Through in die DB (best-effort — bei DB-Ausfall läuft der Chat im RAM weiter).
        try:
            import store
            uid = (self._identity.get(session_id) or {}).get("user_id")
            store.history_append(session_id, role, content, user_key=str(uid) if uid is not None else None)
        except Exception:
            pass

    def reset_history(self, session_id: str) -> None:
        self._history[session_id] = []
        self._summary.pop(session_id, None)
        self._last_tools.pop(session_id, None)
        try:
            import store
            store.history_reset(session_id)
        except Exception:
            pass

    # ── Tool-Ergebnis-Gedächtnis (für Rückfragen zu zuvor Geholtem) ───────────
    def set_last_tools(self, session_id: str, items: list[dict]) -> None:
        """items: [{name, result}] — die Tool-Ergebnisse des letzten Turns (gekappt gespeichert)."""
        if items:
            self._last_tools[session_id] = items

    def get_last_tools(self, session_id: str) -> list[dict]:
        return self._last_tools.get(session_id, [])

    # ── Barge-in: laufenden Turn abbrechen ────────────────────────────────────
    def request_cancel(self, session_id: str) -> None:
        if session_id:
            self._cancel[session_id] = True

    def is_cancelled(self, session_id: str) -> bool:
        return bool(self._cancel.get(session_id))

    def clear_cancel(self, session_id: str) -> None:
        self._cancel.pop(session_id, None)

    # ── Rollierende Zusammenfassung getrimmter alter Turns ────────────────────
    def get_summary(self, session_id: str) -> str:
        return self._summary.get(session_id, "")

    def set_summary(self, session_id: str, text: str) -> None:
        self._summary[session_id] = text

    def last_user(self, session_id: str):
        return self._last_uid.get(session_id)

    def set_last_user(self, session_id: str, user_id) -> None:
        self._last_uid[session_id] = user_id

    async def register(self, ws, session_id: str | None, client_type: str = "browser", name: str = "") -> str:
        sid = session_id or uuid.uuid4().hex[:12]
        self._ws[sid] = ws
        self._meta[sid] = {"type": client_type or "browser", "name": name or sid}
        now = time.time()
        s = self._seen.setdefault(sid, {"first": now})
        s["last"] = now
        # bei Reconnect: aufgelaufene Events nachliefern
        for ev in self._pending.pop(sid, []):
            try:
                await ws.send_json(ev)
            except Exception:
                break
        return sid

    def disconnect(self, session_id: str) -> None:
        self._ws.pop(session_id, None)

    def touch(self, session_id: str, **telemetry) -> None:
        """Lebenszeichen (Heartbeat) inkl. optionaler Telemetrie (room, volume, rssi, fw)."""
        if not session_id:
            return
        s = self._seen.setdefault(session_id, {"first": time.time()})
        s["last"] = time.time()
        for k, v in telemetry.items():
            if v is not None:
                s[k] = v

    def client_type(self, session_id: str | None) -> str | None:
        return self._meta.get(session_id, {}).get("type") if session_id else None

    def set_render(self, session_id: str, mode: str) -> None:
        """Audio-Render-Fähigkeit der Quelle: 'pcm' = braucht server-gestreamtes PCM (ESP),
        'local' = rendert TTS selbst (Pi/Browser)."""
        if session_id in self._meta:
            self._meta[session_id]["render"] = mode

    def render_mode(self, session_id: str | None) -> str:
        return self._meta.get(session_id, {}).get("render", "local") if session_id else "local"

    # ── Client-Capabilities + Aktions-Routing (Request/Response über WS) ──────────
    def set_capabilities(self, session_id: str, caps: list) -> None:
        if session_id in self._meta:
            self._meta[session_id]["capabilities"] = list(caps or [])

    def capabilities(self, session_id: str | None) -> list:
        return self._meta.get(session_id, {}).get("capabilities", []) if session_id else []

    def clients(self) -> list[dict]:
        """Aktuell verbundene Client-Agenten (type='client') mit Capabilities."""
        return [{"session_id": sid, **self._meta[sid]}
                for sid in self._ws if self._meta.get(sid, {}).get("type") == "client"]

    def resolve_target_client(self, session_id: str | None, device: str | None = None) -> str | None:
        """Zielclient bestimmen: explizites Gerät (Name) > die Session selbst (falls Client) >
        der einzige verbundene Client."""
        clients = self.clients()
        if device:
            for c in clients:
                if c["session_id"] == device or (c.get("name", "").lower() == device.lower()):
                    return c["session_id"]
            return None
        if session_id and self._meta.get(session_id, {}).get("type") == "client":
            return session_id
        return clients[0]["session_id"] if len(clients) == 1 else None

    async def call_client(self, session_id: str, action: str, params: dict, timeout: float = 30.0) -> dict:
        """Aktion an einen Client senden und auf dessen Ergebnis warten (Request/Response)."""
        ws = self._ws.get(session_id)
        if ws is None:
            return {"ok": False, "error": "Client nicht verbunden."}
        req_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._calls[req_id] = fut
        try:
            await ws.send_json({"type": "action", "id": req_id, "action": action, "params": params or {}})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Zeitüberschreitung — der Client hat nicht geantwortet."}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            self._calls.pop(req_id, None)

    def resolve_call(self, req_id: str, result: dict) -> None:
        fut = self._calls.get(req_id)
        if fut and not fut.done():
            fut.set_result(result)

    def sessions(self) -> list[dict]:
        return [{"session_id": sid, **self._meta.get(sid, {})} for sid in self._ws]

    def sessions_for_user(self, user_id) -> list[str]:
        """Aktuell VERBUNDENE Sessions, deren erkannte Identität dem user_id entspricht."""
        if user_id is None:
            return []
        return [sid for sid in self._ws if (self._identity.get(sid) or {}).get("user_id") == user_id]

    def is_connected(self, session_id: str | None) -> bool:
        return bool(session_id) and session_id in self._ws

    def devices(self) -> list[dict]:
        """Alle je gesehenen Quellen (online + offline) für die Admin-Geräteliste."""
        now = time.time()
        out = []
        for sid, s in self._seen.items():
            meta = self._meta.get(sid, {})
            ident = self._identity.get(sid) or {}
            last = s.get("last", s.get("first", now))
            out.append({
                "session_id": sid,
                "type": meta.get("type", "?"),
                "name": meta.get("name", sid),
                "render": meta.get("render", "local"),
                "online": sid in self._ws,
                "last_seen": last,
                "ago_s": round(now - last, 1),
                "first_seen": s.get("first", last),
                "room": s.get("room"),
                "volume": s.get("volume"),
                "mic_gain": s.get("mic_gain"),
                "rssi": s.get("rssi"),
                "fw": s.get("fw"),
                "last_speaker": ident.get("username"),
            })
        out.sort(key=lambda d: (not d["online"], d["ago_s"]))
        return out

    def meta(self, session_id: str | None) -> dict:
        return self._meta.get(session_id, {}) if session_id else {}

    async def stream_audio(self, session_id: str, header: dict, pcm: bytes,
                           footer: dict, chunk: int = 3200) -> bool:
        """TTS-PCM (s16le 16k) live an eine Session streamen: header(JSON) → Binär-Frames → footer(JSON).
        Nur für verbundene Quellen (z.B. ESP-Satellit); offline → kein Audio (JSON-Event wird separat gepuffert)."""
        ws = self._ws.get(session_id)
        if ws is None:
            return False
        try:
            await ws.send_text(json.dumps(header))
            for i in range(0, len(pcm), chunk):
                await ws.send_bytes(pcm[i:i + chunk])
            await ws.send_text(json.dumps(footer))
            return True
        except Exception:
            self._ws.pop(session_id, None)
            return False

    async def push(self, session_id: str, event: dict) -> bool:
        """Event an eine bestimmte Session schicken. Offline → puffern (Reconnect liefert nach)."""
        ws = self._ws.get(session_id)
        if ws is not None:
            try:
                await ws.send_json(event)
                return True
            except Exception:
                self._ws.pop(session_id, None)
        self._pending.setdefault(session_id, []).append(event)
        return False


hub = SessionHub()
