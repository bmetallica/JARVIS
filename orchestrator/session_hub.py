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

    def set_identity(self, session_id: str, identity: dict | None) -> None:
        if identity:
            self._identity[session_id] = identity
        else:
            self._identity.pop(session_id, None)

    def get_identity(self, session_id: str | None) -> dict | None:
        return self._identity.get(session_id) if session_id else None

    # ── Verlauf pro Session (vom Server geführt, NICHT vom Client) ──────────────
    def history(self, session_id: str) -> list[dict]:
        return self._history.get(session_id, [])

    def append_history(self, session_id: str, role: str, content: str, limit: int = 20) -> None:
        h = self._history.setdefault(session_id, [])
        h.append({"role": role, "content": content})
        if len(h) > limit:
            del h[:-limit]

    def reset_history(self, session_id: str) -> None:
        self._history[session_id] = []

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
