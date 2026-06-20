"""
JARVIS (SH-Jarvis) — Orchestrator (Tier 1)

FastAPI-Kern, der den GPU-Inferenz-Tier (LLM/STT/TTS) bündelt und ein
Web-UI mit Chat- und Voice-Grundfunktionen bereitstellt.

Start (Entwicklung):
    cd orchestrator
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8000 --reload

Danach im Browser:  http://<host>:8000
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, WebSocket, WebSocketDisconnect, Cookie
from fastapi.responses import Response, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
import services
import tools
import timers
import knowledge
import store
import auth
import biometrics
import mcp_hub
import debug
import automations
import watchers
import skills
import messaging
from session_hub import hub

BASE_DIR   = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="JARVIS Orchestrator", version="0.1.0")


# ── Schemas ──────────────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    session_id: str | None = None


class TTSRequest(BaseModel):
    text: str


# ── API ──────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"orchestrator": "ok", "services": services.health(config.get())}


@app.get("/api/config")
def get_config(jarvis_admin_token: str | None = Cookie(default=None)):
    """Zentrale Einstellungen — nur Admin (verwaltet im Admin-UI)."""
    _admin(jarvis_admin_token)
    return config.get()


@app.post("/api/config")
def set_config(patch: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    return config.update(patch)


@app.get("/api/models")
def models(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    try:
        return {"models": services.list_models(config.get())}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM nicht erreichbar: {e}")


# ── Selbstbedienung im normalen UI (kein Admin nötig) ─────────────────────────

@app.post("/api/voice/enroll-self")
async def enroll_self(file: UploadFile = File(...), session_id: str = Form(...)):
    """Fügt der ZULETZT ERKANNTEN Person dieser Session ein Stimm-Sample hinzu.
    Erst-Enrollment eines neuen Nutzers läuft über das Admin-UI."""
    ident = hub.get_identity(session_id)
    if not ident:
        raise HTTPException(status_code=403, detail="Kein erkannter Nutzer in dieser Session.")
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="Leere Aufnahme.")
    try:
        await asyncio.to_thread(biometrics.enroll, ident["user_id"], audio, file.filename or "voice.webm")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Enrollment fehlgeschlagen: {e}")
    return {"username": ident["username"], "samples": biometrics.count_for_user(ident["user_id"])}


@app.post("/api/users/create-basic")
def create_basic(body: dict):
    """Legt einen passwortlosen Nutzer an (Onboarding). Nur ein bereits erkannter
    Nutzer darf das. Gruppen/Rechte vergibt weiterhin nur ein Admin."""
    ident = hub.get_identity((body or {}).get("session_id"))
    if not ident:
        raise HTTPException(status_code=403, detail="Nur erkannte Nutzer dürfen neue Nutzer anlegen.")
    username = (body or {}).get("username", "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Benutzername nötig.")
    try:
        uid = auth.create_user(username)      # passwortlos
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Anlegen fehlgeschlagen (Name vergeben?): {e}")
    return {"id": uid, "username": username}


@app.post("/api/auth/set-initial-password")
def set_initial_password(body: dict):
    """Erst-Login: Nutzer ohne Passwort vergibt sein eigenes."""
    u = (body or {}).get("username", "").strip()
    p = (body or {}).get("password", "")
    if not auth.needs_initial_password(u):
        raise HTTPException(status_code=400, detail="Für diesen Nutzer ist bereits ein Passwort gesetzt (oder er existiert nicht).")
    if not auth.set_initial_password(u, p):
        raise HTTPException(status_code=400, detail="Passwort zu kurz (min. 4 Zeichen).")
    return {"ok": True}


def _connected_clients_hint() -> str:
    """Verbundene Client-Rechner in den Prompt geben, damit der Agent gezielt einen davon
    ansprechen kann (z.B. „öffne Firefox auf VM")."""
    cs = hub.clients()
    if not cs:
        return ""
    names = ", ".join(c.get("name", "?") for c in cs)
    return (f"\n\nVerbundene Client-Rechner (PCs): {names}. "
            "Soll eine Aktion auf einem bestimmten Rechner laufen, übergib dessen Namen als Parameter "
            "`device` an client_action/client_screenshot (z.B. device=\"VM\"). Nennt der Nutzer einen "
            "Rechner, nutze GENAU diesen Namen. Sind mehrere verbunden und es ist unklar welcher, "
            "frage kurz nach oder nenne die Auswahl.")


def _now_hint() -> str:
    """Aktuelles Datum/Uhrzeit in den Prompt geben — damit Zeit-/Terminangaben (auch beim
    Anlegen von Automatisierungen) korrekt berechnet werden, statt sie zu raten."""
    import datetime as _dt
    wd = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    n = _dt.datetime.now()
    return f"\n\nAktuelles Datum/Uhrzeit: {wd[n.weekday()]}, {n.strftime('%d.%m.%Y %H:%M')} (Europe/Berlin)."


_TOOL_HINT = (
    "\n\nWerkzeuge: Timer/Wecker (set_timer, list_timers, cancel_timer), aktuelle "
    "Zeit/Datum (get_datetime — bei Zeitfragen IMMER aufrufen, nie raten), Wetter "
    "(weather), Web-Suche (web_search — für aktuelle Infos/Fakten/News) und Seitenabruf "
    "(fetch_url — den INHALT einer konkreten Seite holen, z.B. Schlagzeilen von einer Newsseite; "
    "für News bevorzugt fetch_url statt nur web_search). "
    "Für gründliche/umfassende Fragen (Hintergründe, Vergleiche, Lage) den Recherche-Agenten research nutzen "
    "(sucht + ruft mehrere Quellen ab + fasst mit Quellen zusammen). Bilder per analyze_image (multimodal) auswerten. "
    "Für JavaScript-Seiten oder interaktive Aktionen (Suche/Login/Klicken) browse + browser_click/browser_type "
    "nutzen (echter headless-Browser; fetch_url nur für statisches HTML). "
    "Soll etwas auf dem NUTZER-PC passieren (Programm starten, Fenster, Lautstärke/Medien, Dateien/Zwischenablage), "
    "client_action verwenden. Bei mehreren verbundenen Rechnern den Ziel-Rechner über `device` wählen — nennt der "
    "Nutzer einen (z.B. 'auf VM'), exakt diesen Namen als device übergeben; mit list_clients siehst du die Namen. "
    "Persönliche Fakten des Nutzers SOFORT und STILL mit save_memory speichern (nicht ankündigen). "
    "Bei Fragen zu eigenen Dokumenten/Unterlagen knowledge_search nutzen. "
    "Für wiederkehrende oder geplante Aufgaben, an die JARVIS SELBSTSTÄNDIG denken soll "
    "('erinnere mich täglich…', 'prüfe stündlich… und sag Bescheid wenn…', 'wenn X erkannt wird, …'), "
    "create_automation nutzen (list_automations/update_automation/cancel_automation zum Verwalten). "
    "Weist der Nutzer auf einen Fehler in einer bestehenden Aufgabe hin (z.B. falsch geschriebene Domain), "
    "korrigiere sie mit update_automation (vorher ggf. list_automations für ID/Text). "
    "Unterscheide: set_timer = einmaliger kurzer Wecker JETZT; create_automation = wiederkehrend/geplant/ereignisbasiert. "
    "Für Berechnungen, Datenverarbeitung, Datei-/Diagramm-/PDF-Erzeugung oder eigene Helfer-Skripte run_python "
    "(isolierte Sandbox) nutzen — rechne/rate solche Dinge NICHT selbst, sondern führe Code aus und nutze dessen Ausgabe. "
    "Erfinde NIEMALS Fakten (Wetter, Uhrzeit, Suchergebnisse) — nutze immer das Werkzeug. "
    "Wenn ein Werkzeug 'Berechtigung verweigert' zurückgibt, sage der Person ehrlich, dass sie dazu keine Berechtigung hat. "
    "Rufe das passende Werkzeug auf und antworte danach kurz auf Deutsch."
)

MAX_TOOL_STEPS = 8     # mehrstufige MCP-Abfragen (Gerät suchen → Wert lesen → …) brauchen mehr Schritte


async def _prepare_turn(req: ChatRequest):
    """Gemeinsame Vorbereitung für Stream- und Nicht-Stream-Chat:
    Identität, Sprecherwechsel-Reset, System-Prompt (+Recall), Verlauf, Tools."""
    cfg = config.get()
    sid = req.session_id or "anon"
    identity = hub.get_identity(sid)          # pro Äußerung aus der Stimme
    user_id = identity["user_id"] if identity else None
    if user_id != hub.last_user(sid):         # Sprecherwechsel → Kontext + Onboarding zurücksetzen
        hub.reset_history(sid)
        hub.set_last_user(sid, user_id)
        hub.set_onboarding_state(sid, None)

    namespace = f"u{user_id}" if user_id else "guest"
    ctx = {"session_id": sid, "cfg": cfg, "namespace": namespace, "user_id": user_id}

    # ── Deterministisches Onboarding bei unbekannter Stimme ────────────────────
    onboarding_q = None
    answering_onboarding = False
    guest_voice = identity is None and hub.get_last_voice(sid) is not None
    if identity is not None:
        hub.set_onboarding_state(sid, None)
    elif guest_voice:
        st = hub.onboarding_state(sid)
        if st is None:
            hub.set_onboarding_state(sid, "asked")
            hub.reset_history(sid)         # ursprüngliche Frage nicht in die Registrierung mitschleppen
            onboarding_q = ("Ich erkenne deine Stimme nicht. Bist du schon registriert? "
                            "Wenn ja, sag mir bitte deinen Namen. Wenn nein, sag 'neues Profil' und deinen Namen.")
        elif st == "asked":
            answering_onboarding = True
            hub.set_onboarding_state(sid, "skipped")

    system = cfg["system_prompt"] + _TOOL_HINT + _now_hint() + _connected_clients_hint()
    if identity:
        system += (f"\n\nAktueller Sprecher (per Stimme erkannt): {identity['username']}. "
                   "Beantworte Fragen zur Person und die Frage wer-bin-ich AUSSCHLIESSLICH anhand dieser Identität "
                   "und des Gedächtnisses dieser Person — ignoriere abweichende Angaben aus früheren Nachrichten. "
                   "Sprich die Person mit Namen an.")
    elif answering_onboarding:
        system += ("\n\nDer unbekannte Sprecher antwortet gerade auf die Frage, ob er registriert ist. "
                   "Sagt er JA und nennt einen Namen → rufe link_voice_to_existing_user(username) auf. "
                   "Sagt er NEIN / 'neues Profil' und nennt einen Namen → rufe create_user(username) auf "
                   "(die Stimme wird automatisch hinterlegt). Stellt er stattdessen eine normale Frage, beantworte sie.")
    else:
        system += ("\n\nDer Sprecher ist nicht identifiziert (Gast). Wenn nach der Identität gefragt wird, "
                   "sage ehrlich, dass du nicht sicher weißt, wer gerade spricht.")

    # ── Kanal-Bewusstsein: über welches Gerät wird gesprochen? ─────────────────
    channel = (hub.meta(sid) or {}).get("type", "browser")
    if channel == "satellite":
        system += ("\n\nKANAL: Du sprichst über einen reinen SPRACH-SATELLITEN (nur Audio, kein Bildschirm, "
                   "keine Tastatur, kein Browser-Fenster). Antworte gesprochen, knapp und natürlich. "
                   "Öffne oder zeige NIEMALS Apps, Browser, Fenster, Bilder, Tabellen oder Links — das Gerät hat keinen Bildschirm. "
                   "Nenne Ergebnisse direkt als gesprochenen Satz (Wetter, Uhrzeit, Smart-Home, kurze Web-Antworten). "
                   "Vermeide lange Aufzählungen, URLs und Formatierung.")
        ctx["channel"] = "satellite"
    try:
        mems = await asyncio.to_thread(knowledge.recall_memory, cfg, req.message, namespace)
        if mems:
            system += "\n\nBekanntes über die Person (aus dem Gedächtnis):\n" + \
                      "\n".join(f"- {m['content']}" for m in mems)
    except Exception:
        pass

    system += skills.catalog_hint()           # deferred: nur Namen+Beschreibung der vorhandenen Skills
    working = [{"role": "system", "content": system}] + list(hub.history(sid)) + \
              [{"role": "user", "content": req.message}]
    available_tools = tools.TOOL_SCHEMAS + mcp_hub.tool_schemas()
    # Denken steuerbar:
    #   auto     = Onboarding/Identität ODER MCP-Tools verfügbar (mehrstufig → Reasoning)
    #   adaptive = erst ohne Denken (schnell); bei Fehlschlag automatisch mit Denken wiederholen
    #   always / never = erzwingen
    mode = cfg.get("thinking_mode", "adaptive")
    if mode == "always":
        think = True
    elif mode in ("never", "adaptive"):
        think = False                 # adaptive startet ohne Denken (1. Versuch)
    else:                             # auto
        has_mcp = any(t["function"]["name"].startswith("mcp__") for t in available_tools)
        think = guest_voice or has_mcp
    if answering_onboarding:
        think = True                  # Registrierungs-Antwort zuverlässig (mit Denken) verarbeiten
    return cfg, sid, ctx, identity, working, available_tools, think, mode, onboarding_q


async def _run_loop(cfg: dict, ctx: dict, base_working: list, available_tools: list, think: bool) -> dict:
    """Tool-Loop (nicht streamend) auf einer Kopie des Verlaufs. Gibt {ok, content} zurück.
    ok=False bei: zu vielen Schritten, leerer Antwort oder LLM-Fehler (→ Trigger für adaptiven Retry)."""
    working = list(base_working)
    for _ in range(MAX_TOOL_STEPS):
        t0 = time.time()
        tools_now = available_tools + skills.schemas_for(ctx.get("loaded_skills"))   # deferred: geladene Skills dazu
        try:
            res = await asyncio.to_thread(services.llm_call, working, cfg, tools_now, think)
        except Exception as e:
            debug.log("llm_error", think=think, error=str(e)[:200])
            return {"ok": False, "content": "", "error": str(e)}
        debug.log("llm", think=think, ms=int((time.time() - t0) * 1000),
                  tool_calls=[t["name"] for t in res["tool_calls"]], content_len=len(res["content"]))
        if res["tool_calls"]:
            working.append(res["raw"])
            for tc in res["tool_calls"]:
                result = await tools.execute_tool(tc["name"], tc["args"], ctx)
                working.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
            continue
        return {"ok": bool(res["content"]), "content": res["content"]}
    return {"ok": False, "content": ""}


async def _run_chat(req: ChatRequest, channel: str = "chat") -> tuple[str, dict | None]:
    """Voller Chat-Turn (Onboarding + adaptiver Tool-Loop). Gibt (Antworttext, identity) zurück.
    Wird von /api/chat UND vom Satelliten-Audio-Endpoint genutzt."""
    cfg, sid, ctx, identity, base_working, available_tools, think, mode, onboarding_q = await _prepare_turn(req)
    debug.log("turn", channel=channel, session=sid, user=(identity or {}).get("username"),
              namespace=ctx["namespace"], mode=mode, think=think, message=req.message[:300])
    if onboarding_q:
        hub.append_history(sid, "assistant", onboarding_q)
        debug.log("turn_done", channel=channel, onboarding=True, reply=onboarding_q[:120])
        return onboarding_q, identity
    t0 = time.time()
    r = await _run_loop(cfg, ctx, base_working, available_tools, think)
    retried = False
    if mode == "adaptive" and not r["ok"]:
        debug.log("retry", reason="adaptive: 1. Versuch ohne Denken ohne Ergebnis")
        r = await _run_loop(cfg, ctx, base_working, available_tools, True)
        retried = True
    content = r["content"] or "Entschuldige, das hat nicht geklappt — bitte versuche es nochmal."
    hub.append_history(sid, "user", req.message)
    hub.append_history(sid, "assistant", content)
    debug.log("turn_done", channel=channel, ms=int((time.time() - t0) * 1000),
              retried=retried, reply=content[:300])
    return content, identity


@app.post("/api/chat")
async def chat(req: ChatRequest):
    content, identity = await _run_chat(req, channel="chat")
    return {"reply": content, "speaker": identity}


def _split_sentences(text: str) -> list[str]:
    return [p.strip() for p in services._SENT_END.split(text) if p.strip()]


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """Streamt die finale Antwort satzweise (SSE) → TTS startet sofort.
    Im adaptive-Modus: erst schneller Versuch ohne Denken; klappt der nicht,
    zweiter Versuch MIT Denken (dann live gestreamt)."""
    import json as _json
    cfg, sid, ctx, identity, base_working, available_tools, think, mode, onboarding_q = await _prepare_turn(req)
    debug.log("turn", channel="stream", session=sid, user=(identity or {}).get("username"),
              namespace=ctx["namespace"], mode=mode, think=think, message=req.message[:300])
    _t0 = time.time()

    def _sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {_json.dumps(payload, ensure_ascii=False)}\n\n"

    async def _stream_pass(use_think: bool):
        """Streamt einen Lauf MIT Tool-Loop. Yields SSE-Strings; setzt self._content am Ende."""
        loop = asyncio.get_running_loop()
        working = list(base_working)
        for _ in range(MAX_TOOL_STEPS):
            q: asyncio.Queue = asyncio.Queue()
            tools_now = available_tools + skills.schemas_for(ctx.get("loaded_skills"))   # deferred: geladene Skills

            def produce():
                try:
                    for ev in services.llm_stream(working, cfg, tools_now, use_think):
                        loop.call_soon_threadsafe(q.put_nowait, ev)
                except Exception as e:
                    loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "detail": str(e)})
                loop.call_soon_threadsafe(q.put_nowait, None)

            import threading as _th
            _th.Thread(target=produce, daemon=True).start()

            done = None
            while True:
                ev = await q.get()
                if ev is None:
                    break
                if ev["type"] == "sentence":
                    yield ("sentence", ev["text"])
                elif ev["type"] == "error":
                    yield ("error", ev["detail"]); return
                elif ev["type"] == "done":
                    done = ev
            if done and done["tool_calls"]:
                working.append(done["raw"])
                for tc in done["tool_calls"]:
                    result = await tools.execute_tool(tc["name"], tc["args"], ctx)
                    working.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                continue
            yield ("final", (done or {}).get("content", "")); return
        yield ("final", "")

    async def gen():
        final = ""
        # ── Deterministische Onboarding-Frage (kein LLM) ───────────────────────
        if onboarding_q:
            yield _sse("sentence", {"text": onboarding_q})
            hub.append_history(sid, "assistant", onboarding_q)   # nur die Frage
            debug.log("turn_done", channel="stream", onboarding=True, reply=onboarding_q[:120])
            yield _sse("done", {"content": onboarding_q, "speaker": identity})
            return
        # ── adaptive: schneller Vorab-Versuch ohne Denken ──────────────────────
        if mode == "adaptive":
            r = await _run_loop(cfg, ctx, base_working, available_tools, False)
            if r["ok"]:
                for s in _split_sentences(r["content"]):
                    yield _sse("sentence", {"text": s})
                final = r["content"]
                hub.append_history(sid, "user", req.message)
                hub.append_history(sid, "assistant", final)
                debug.log("turn_done", channel="stream", ms=int((time.time() - _t0) * 1000),
                          retried=False, reply=final[:300])
                yield _sse("done", {"content": final, "speaker": identity})
                return
            debug.log("retry", reason="adaptive: 1. Versuch ohne Denken ohne Ergebnis")
            use_think = True            # Fehlschlag → zweiter Versuch MIT Denken (gestreamt)
        else:
            use_think = think

        async for kind, data in _stream_pass(use_think):
            if kind == "sentence":
                yield _sse("sentence", {"text": data})
            elif kind == "error":
                yield _sse("error", {"detail": data}); return
            elif kind == "final":
                final = data
        hub.append_history(sid, "user", req.message)
        hub.append_history(sid, "assistant", final)
        debug.log("turn_done", channel="stream", ms=int((time.time() - _t0) * 1000),
                  retried=(mode == "adaptive"), reply=final[:300])
        yield _sse("done", {"content": final, "speaker": identity})

    return StreamingResponse(gen(), media_type="text/event-stream")


# ── WebSocket: quellen-bezogenes I/O-Routing (Timer-Alarme etc.) ──────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    sid = None
    try:
        hello = await websocket.receive_json()
        sid = await hub.register(
            websocket,
            hello.get("session_id"),
            hello.get("client_type", "browser"),
            hello.get("name", ""),
        )
        await websocket.send_json({"type": "welcome", "session_id": sid})
        m = hub.meta(sid)
        automations.emit("device_connected", {"type": m.get("type"), "name": m.get("name"), "session_id": sid})
        while True:
            await websocket.receive_text()      # Keepalive; eingehende Nachrichten ignorieren
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if sid:
            m = hub.meta(sid)
            hub.disconnect(sid)
            automations.emit("device_disconnected", {"type": m.get("type"), "name": m.get("name"), "session_id": sid})


# ── Audio-Satellit (ESP32): WebSocket mit Binär-Audio rein/raus ───────────────

def _pcm16k_to_wav(pcm: bytes, sr: int = 16000) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def _decode_to_pcm16k(data: bytes) -> bytes:
    """Beliebiges TTS-Audio (mp3/wav) → rohes s16le mono 16 kHz (für den ESP-I2S-DAC)."""
    import subprocess
    p = subprocess.run(
        ["ffmpeg", "-loglevel", "quiet", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"],
        input=data, capture_output=True)
    return p.stdout


async def _satellite_turn(ws: WebSocket, sid: str, pcm: bytes) -> None:
    cfg = config.get()
    if len(pcm) < 16000 * 2 // 2:                      # < ~0.5 s Audio → ignorieren
        await ws.send_text(json.dumps({"type": "state", "state": "idle"})); return
    wav = _pcm16k_to_wav(pcm)
    # STT + Sprecher-Erkennung (aus demselben Audio)
    try:
        text = await asyncio.to_thread(services.transcribe, wav, "speech.wav", cfg)
    except Exception as e:
        await ws.send_text(json.dumps({"type": "error", "detail": f"STT: {e}"})); return
    try:
        thr = float(cfg.get("voice_id_threshold", 0.65))
        emb = await asyncio.to_thread(biometrics.embed_audio, wav, "speech.wav")
        spk = await asyncio.to_thread(biometrics.identify_by_embedding, emb, thr)
        hub.set_last_voice(sid, emb)
    except Exception:
        spk = None
    hub.set_identity(sid, spk)
    if spk:
        asyncio.create_task(automations.manager.dispatch_event(
            "speaker_recognized",
            {"username": spk.get("username"), "user_id": spk.get("user_id"), "session_id": sid}))
    await ws.send_text(json.dumps({"type": "transcript", "text": text,
                                   "speaker": (spk or {}).get("username")}))
    if not text.strip():
        await ws.send_text(json.dumps({"type": "state", "state": "idle"})); return
    await ws.send_text(json.dumps({"type": "state", "state": "thinking"}))
    reply, _ = await _run_chat(ChatRequest(message=text, session_id=sid), channel="satellite")
    # TTS → PCM 16 kHz → in Frames zurückstreamen
    await ws.send_text(json.dumps({"type": "state", "state": "speaking"}))
    try:
        audio, _mt = await asyncio.to_thread(services.synthesize, reply, cfg)
        out = await asyncio.to_thread(_decode_to_pcm16k, audio)
    except Exception as e:
        await ws.send_text(json.dumps({"type": "error", "detail": f"TTS: {e}"})); return
    await ws.send_text(json.dumps({"type": "tts_start", "sr": 16000, "text": reply[:300]}))
    CHUNK = 3200                                        # 100 ms @16k/16bit
    # Getaktet senden (kein Burst): der ESP kann den TLS-Empfang sonst nicht schnell genug leeren
    # → WS-Abbruch mitten im TTS. ~60 ms/100-ms-Chunk = leichter Vorlauf, glatte Wiedergabe.
    for i in range(0, len(out), CHUNK):
        await ws.send_bytes(out[i:i + CHUNK])
        await asyncio.sleep(0.06)
    await ws.send_text(json.dumps({"type": "tts_end"}))
    await ws.send_text(json.dumps({"type": "state", "state": "idle"}))


async def _speak_to_satellite(sid: str, text: str) -> bool:
    """Server-initiierte Sprachausgabe an einen verbundenen Satelliten (z.B. Timer-Alarm).
    Synthetisiert TTS → PCM 16 kHz → streamt tts_start / Binär-Frames / tts_end über den Push-Kanal."""
    cfg = config.get()
    try:
        audio, _mt = await asyncio.to_thread(services.synthesize, text, cfg)
        pcm = await asyncio.to_thread(_decode_to_pcm16k, audio)
    except Exception as e:
        print(f"[alarm-tts] {e}")
        return False
    return await hub.stream_audio(
        sid,
        {"type": "tts_start", "sr": 16000, "text": text[:200]},
        pcm,
        {"type": "tts_end"},
    )


async def announce(session_id: str, text: str, *, kind: str = "notify", **meta) -> dict:
    """Universeller Rückkanal an die Ursprungs-Session — kanal-bewusst.

    Genutzt von allen server-initiierten Ausgaben: Timern, Weckern, Automatisierungen,
    Cronjobs, MCP-/Smarthome-Events usw. Der Aufrufer muss den Gerätetyp NICHT kennen.
      • Browser/Client: JSON-Event ``{type: kind, message: text, **meta}`` (UI zeigt/spricht es,
        offline → gepuffert und beim Reconnect nachgeliefert).
      • ESP-Satellit:   zusätzlich gesprochene TTS über den Audio-Push-Kanal (Beep + Sprache).
    Rückgabe: ``{"delivered": bool, "spoken": bool}``.
    """
    event = {"type": kind, "message": text, **meta}
    delivered = await hub.push(session_id, event)
    spoken = False
    # Server-gestreamtes PCM nur an Quellen, die TTS NICHT selbst rendern (ESP-Satellit).
    # Pi/Browser bekommen nur das JSON-Event und sprechen es lokal (kein Doppel-TTS).
    if hub.render_mode(session_id) == "pcm":
        spoken = await _speak_to_satellite(session_id, text)
    return {"delivered": delivered, "spoken": spoken}


# ── Autonomie: geplante/ereignisgesteuerte Selbstläufe ────────────────────────

async def _run_automation(autom: dict, payload: dict | None) -> str:
    """Runner für den AutomationManager. Watcher = günstiger Skript-Check, LLM nur bei Treffer;
    sonst klassischer autonomer Agenten-Tool-Loop."""
    if autom.get("kind") == "watcher":
        return await _run_watcher(autom, payload)
    return await _automation_llm_run(autom, payload)


async def _automation_llm_run(autom: dict, payload: dict | None, extra_user: str | None = None) -> str:
    """Führt die Aufgabe AUTONOM im Agenten-Tool-Loop aus (Besitzer-Rechte + Autonomie-Blacklist)."""
    cfg = config.get()
    uid = autom.get("owner_user_id")
    namespace = f"u{uid}" if uid else "guest"
    sid = (payload or {}).get("session_id") or autom.get("target_session") or "autonomous"
    ctx = {"session_id": sid, "cfg": cfg, "namespace": namespace, "user_id": uid, "autonomous": True}

    channel = (hub.meta(sid) or {}).get("type", "browser")
    sat = (" Du meldest dich über einen reinen Sprach-Satelliten: knapp, gesprochen, "
           "keine Links/Listen/Formatierung.") if channel == "satellite" else ""
    ctxinfo = ("\nKontext des auslösenden Ereignisses: " + json.dumps(payload, ensure_ascii=False)) if payload else ""
    system = (cfg["system_prompt"] +
              "\n\nDu führst gerade eine GEPLANTE, AUTONOME Aufgabe aus — es wartet NIEMAND live auf eine Antwort. "
              "Erledige sie selbstständig mit den verfügbaren Werkzeugen. "
              "Gibt es nach Erledigung NICHTS Berichtenswertes für den Nutzer, antworte AUSSCHLIESSLICH mit dem Wort "
              "SILENT. Andernfalls formuliere eine kurze, natürliche deutsche Meldung, die dem Nutzer proaktiv "
              "mitgeteilt wird." + _now_hint() + sat + ctxinfo + skills.catalog_hint())
    user_msg = autom["task"] + (("\n\n" + extra_user) if extra_user else "")
    working = [{"role": "system", "content": system}, {"role": "user", "content": user_msg}]
    available = tools.TOOL_SCHEMAS + mcp_hub.tool_schemas()
    debug.log("automation_run", id=autom["id"], title=autom["title"], user=uid, session=sid,
              task=autom["task"][:200])
    r = await _run_loop(cfg, ctx, working, available, think=True)
    content = (r.get("content") or "").strip()
    debug.log("automation_done", id=autom["id"], ok=r.get("ok"), reply=content[:200])
    return content


async def _run_watcher(autom: dict, payload: dict | None) -> str:
    """Watcher-Lauf: Prüfskript billig in der Sandbox; LLM nur bei echtem Treffer.
    Mutiert `autom` (state/fail_count/check_script) — der AutomationManager speichert danach."""
    uid = autom.get("owner_user_id")
    namespace = f"u{uid}" if uid else "guest"
    chk = await asyncio.to_thread(watchers.run_check, autom, namespace)

    if not chk["ok"]:
        autom["fail_count"] = autom.get("fail_count", 0) + 1
        debug.log("watcher_error", id=autom["id"], fails=autom["fail_count"], error=chk["error"][:200])
        if autom["fail_count"] < watchers.FAIL_THRESHOLD:
            return automations.SILENT_TOKEN                      # unter Schwelle → still abwarten
        if await _repair_watcher(autom, chk["error"]):          # Self-Heal versuchen
            debug.log("watcher_healed", id=autom["id"])
            return automations.SILENT_TOKEN
        autom["enabled"] = False                                # nicht reparierbar → pausieren + melden
        return (f"Die Überwachung „{autom['title']}“ funktioniert nicht mehr und ließ sich nicht automatisch "
                f"reparieren — ich habe sie pausiert. Letzter Fehler: {chk['error'][:200]}")

    autom["fail_count"] = 0
    parsed = chk["parsed"]
    autom["state"] = parsed.get("state") if isinstance(parsed.get("state"), dict) else (autom.get("state") or {})
    debug.log("watcher_check", id=autom["id"], triggered=bool(parsed.get("triggered")))
    if not parsed.get("triggered"):
        return automations.SILENT_TOKEN                          # keine Änderung → kein LLM, keine Meldung

    summary = (parsed.get("summary") or "").strip()
    msg = (await _automation_llm_run(autom, payload, extra_user=(
        "Die Überwachung hat ausgelöst. Neue/relevante Information:\n" + (summary or "(ohne Detail)") +
        "\n\nFühre die Aufgabe aus und melde dem Nutzer das Ergebnis kurz und natürlich.")) or "").strip()
    # Ein Treffer wird IMMER gemeldet — liefert der LLM-Lauf nichts/SILENT, nutze die Roh-Zusammenfassung.
    if not msg or msg.upper().strip(".!") == automations.SILENT_TOKEN:
        msg = summary or f"Die Überwachung „{autom['title']}“ hat ausgelöst."
    return msg


async def _repair_watcher(autom: dict, error: str) -> bool:
    """Lässt das LLM das defekte Prüfskript neu schreiben und testet es. True = repariert."""
    cfg = config.get()
    uid = autom.get("owner_user_id")
    namespace = f"u{uid}" if uid else "guest"
    system = (cfg["system_prompt"] + "\n\n" + watchers.SCRIPT_CONTRACT +
              "\n\nEin bestehendes Überwachungs-Skript ist FEHLERHAFT. Schreibe es KORRIGIERT neu. "
              "Antworte AUSSCHLIESSLICH mit dem reinen Python-Skript — kein Markdown, keine Erklärung.")
    user = (f"Ziel der Überwachung: {autom.get('task')}\n\nAktuelles (fehlerhaftes) Skript:\n"
            f"{autom.get('check_script')}\n\nFehler beim letzten Lauf:\n{error}")
    ctx = {"session_id": "autonomous", "cfg": cfg, "namespace": namespace, "user_id": uid, "autonomous": True}
    r = await _run_loop(cfg, ctx, [{"role": "system", "content": system},
                                   {"role": "user", "content": user}], tools.TOOL_SCHEMAS, think=True)
    new_script = watchers.strip_code_fences((r.get("content") or "").strip())
    if not new_script:
        return False
    test = await asyncio.to_thread(watchers.run_check, {**autom, "check_script": new_script}, namespace)
    if test.get("ok"):
        autom["check_script"] = new_script
        autom["fail_count"] = 0
        if isinstance(test["parsed"].get("state"), dict):
            autom["state"] = test["parsed"]["state"]
        return True
    return False


async def _deliver_automation(autom: dict, text: str, payload: dict | None) -> None:
    """Ergebnis einer Automatisierung melden — an die Zielquelle UND an aktuell verbundene
    Sessions des Besitzers (robust, falls die ursprüngliche Session-ID offline ist)."""
    targets = set()
    sid = (payload or {}).get("session_id") or autom.get("target_session")
    if sid:
        targets.add(sid)                                     # ggf. gepuffert, falls offline
    targets.update(hub.sessions_for_user(autom.get("owner_user_id")))
    for t in targets:
        await announce(t, text, kind="notify", automation=autom["title"])
    # Zusätzlich zuverlässig per Messaging (Telegram) an den Besitzer — geräteunabhängig.
    if messaging.enabled() and autom.get("owner_user_id") is not None:
        await asyncio.to_thread(messaging.send_to_user, autom["owner_user_id"],
                                f"🤖 {autom['title']}: {text}")


async def _handle_telegram_message(chat_id, text: str, sender: str = "") -> None:
    """Eingehende Telegram-Nachricht → NUR für verifizierte Kontakte: Agenten-Turn + Antwort.
    Unbekannte/nicht zugeordnete Absender werden ausschließlich als „ausstehend" protokolliert —
    kein Agentenlauf, KEINE Antwort (Sicherheits-Anforderung)."""
    text = (text or "").strip()
    if not text:
        return
    # SICHERHEIT: strikte Allowlist — nur verifizierte Chat-IDs werden überhaupt verarbeitet.
    if not await asyncio.to_thread(messaging.is_verified, chat_id):
        messaging.add_pending(chat_id, sender, text)
        debug.log("telegram_blocked", chat=chat_id, sender=sender, message=text[:120])
        print(f"[telegram] Nachricht von nicht verifiziertem Chat {chat_id} ignoriert (→ ausstehende Kontakte).")
        return
    sid = f"tg{chat_id}"
    user = await asyncio.to_thread(messaging.user_for_chat, chat_id)
    if user:                                              # Identität = zugeordneter Nutzer
        hub.set_identity(sid, {"user_id": user["id"], "username": user["username"], "confidence": 1.0})
    debug.log("telegram_in", chat=chat_id, user=(user or {}).get("username"), message=text[:200])
    try:
        reply, _ = await _run_chat(ChatRequest(message=text, session_id=sid), channel="telegram")
    except Exception as e:
        reply = f"Fehler: {e}"
    await asyncio.to_thread(messaging.send_to_chat, chat_id, reply)


async def _telegram_poller() -> None:
    """Dauerhafter Long-Poll-Loop; idlet, solange Telegram deaktiviert ist (Live-Toggle möglich)."""
    offset = 0
    while True:
        if not messaging.enabled():
            await asyncio.sleep(5)
            continue
        try:
            updates = await asyncio.to_thread(messaging.get_updates, offset, 25)
        except Exception:
            await asyncio.sleep(5)
            continue
        for u in updates:
            offset = u.get("update_id", offset) + 1
            m = u.get("message") or u.get("edited_message") or {}
            chat = (m.get("chat") or {}).get("id")
            txt = m.get("text")
            frm = m.get("from") or {}
            sender = (frm.get("username") and "@" + frm["username"]) or \
                     " ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])) or ""
            if chat and txt:
                await _handle_telegram_message(chat, txt, sender)


@app.websocket("/ws/satellite")
async def ws_satellite(websocket: WebSocket):
    """ESP32-Audio-Satellit: JSON-Steuerung + Binär-Audio über EINEN Socket.
      ESP→Server: hello / audio_start / <binär PCM s16le 16k> / audio_end / heartbeat
      Server→ESP: welcome / transcript / state{listening|thinking|speaking|idle} /
                  tts_start / <binär PCM> / tts_end / set_volume / timer_alarm / notify
    """
    await websocket.accept()
    sid = None
    buf = bytearray()
    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            txt = msg.get("text")
            if txt is not None:
                try:
                    data = json.loads(txt)
                except Exception:
                    continue
                mt = data.get("type")
                if mt == "hello":
                    sid = await hub.register(websocket, data.get("session_id"),
                                             "satellite", data.get("name", "Satellit"))
                    hub.set_render(sid, "pcm")        # ESP rendert kein TTS selbst → Server streamt PCM
                    hub.touch(sid, room=data.get("name"), fw=data.get("fw"),
                              volume=data.get("volume"))
                    await websocket.send_text(json.dumps({"type": "welcome", "session_id": sid}))
                    automations.emit("device_connected", {"type": "satellite", "name": data.get("name"),
                                                          "room": data.get("name"), "session_id": sid})
                elif mt == "audio_start":
                    buf = bytearray()
                    automations.emit("satellite_listening", {"name": (hub.meta(sid) or {}).get("name"),
                                                             "room": (hub.meta(sid) or {}).get("name"),
                                                             "session_id": sid})
                elif mt == "audio_end":
                    if sid:
                        await _satellite_turn(websocket, sid, bytes(buf))
                    buf = bytearray()
                elif mt == "heartbeat":
                    # Lebenszeichen + Telemetrie → Admin-Geräteliste
                    hub.touch(sid, room=data.get("room"), volume=data.get("volume"),
                              mic_gain=data.get("mic_gain"), rssi=data.get("rssi"), fw=data.get("fw"))
            else:
                b = msg.get("bytes")
                if b:
                    buf.extend(b)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[ws_satellite] {e}")
    finally:
        if sid:
            m = hub.meta(sid)
            hub.disconnect(sid)
            automations.emit("device_disconnected", {"type": "satellite", "name": m.get("name"),
                                                     "room": m.get("name"), "session_id": sid})


@app.websocket("/ws/client")
async def ws_client(websocket: WebSocket):
    """Client-Agent (Multi-OS-Sidecar): registriert sich mit Capabilities; der Server kann
    Aktionen anfordern (Request/Response). Protokoll:
      Client→Server: hello{name,capabilities} · action_result{id,ok,result|error} · heartbeat{...}
      Server→Client: welcome{session_id} · action{id,action,params}
    """
    await websocket.accept()
    sid = None
    try:
        while True:
            data = await websocket.receive_json()
            mt = data.get("type")
            if mt == "hello":
                sid = await hub.register(websocket, data.get("session_id"), "client",
                                         data.get("name", "Client"))
                hub.set_capabilities(sid, data.get("capabilities", []))
                hub.touch(sid, room=data.get("name"), fw=data.get("fw"))
                await websocket.send_json({"type": "welcome", "session_id": sid})
                automations.emit("device_connected", {"type": "client", "name": data.get("name"),
                                                      "session_id": sid})
            elif mt == "action_result":
                hub.resolve_call(data.get("id"), {"ok": data.get("ok", False),
                                                  "result": data.get("result"), "error": data.get("error")})
            elif mt == "heartbeat":
                hub.touch(sid, room=data.get("room"), fw=data.get("fw"))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[ws_client] {e}")
    finally:
        if sid:
            m = hub.meta(sid)
            hub.disconnect(sid)
            automations.emit("device_disconnected", {"type": "client", "name": m.get("name"),
                                                     "session_id": sid})


# ── Wissensbasis (RAG) ────────────────────────────────────────────────────────

def _extract_text(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages)
    return data.decode("utf-8", errors="replace")


@app.post("/api/knowledge/upload")
async def knowledge_upload(file: UploadFile = File(...)):
    cfg = config.get()
    data = await file.read()
    try:
        text = await asyncio.to_thread(_extract_text, file.filename or "", data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Datei nicht lesbar: {e}")
    if not text.strip():
        raise HTTPException(status_code=400, detail="Kein Text in der Datei gefunden.")
    try:
        n = await asyncio.to_thread(knowledge.ingest_document, cfg, file.filename or "dokument", text)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Indexierung fehlgeschlagen: {e}")
    automations.emit("document_uploaded", {"source": file.filename, "namespace": "default", "chunks": n})
    return {"source": file.filename, "chunks": n}


@app.get("/api/knowledge/list")
def knowledge_list():
    try:
        return {"documents": store.list_sources("document")}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"DB-Fehler: {e}")


@app.post("/api/knowledge/delete")
def knowledge_delete(body: dict):
    src = (body or {}).get("source", "")
    if not src:
        raise HTTPException(status_code=400, detail="Kein 'source' angegeben.")
    try:
        removed = store.delete_source(src)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"DB-Fehler: {e}")
    return {"source": src, "removed": removed}


# ── Admin / Auth ──────────────────────────────────────────────────────────────

def _sess(token):
    s = auth.session(token)
    if not s:
        raise HTTPException(status_code=401, detail="Nicht angemeldet.")
    return s


def _admin(token):
    s = _sess(token)
    if not s.get("is_admin"):
        raise HTTPException(status_code=403, detail="Nur für Administratoren.")
    return s


@app.post("/api/admin/login")
def admin_login(body: dict):
    res = auth.login((body or {}).get("username", ""), (body or {}).get("password", ""))
    if not res:
        raise HTTPException(status_code=401, detail="Benutzername oder Passwort falsch.")
    resp = JSONResponse({"username": res["username"], "is_admin": res["is_admin"],
                         "must_change": res["must_change"]})
    resp.set_cookie("jarvis_admin_token", res["token"], httponly=True, samesite="lax", max_age=8 * 3600)
    return resp


@app.post("/api/admin/logout")
def admin_logout(jarvis_admin_token: str | None = Cookie(default=None)):
    if jarvis_admin_token:
        auth.logout(jarvis_admin_token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("jarvis_admin_token")
    return resp


@app.get("/api/admin/me")
def admin_me(jarvis_admin_token: str | None = Cookie(default=None)):
    s = _sess(jarvis_admin_token)
    return {"username": s["username"], "is_admin": s["is_admin"], "must_change": s.get("must_change", False)}


@app.post("/api/admin/change-password")
def admin_change_pw(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    s = _sess(jarvis_admin_token)
    new = (body or {}).get("new_password", "")
    if len(new) < 4:
        raise HTTPException(status_code=400, detail="Passwort zu kurz (min. 4 Zeichen).")
    auth.change_password(s["user_id"], new)
    return {"ok": True}


@app.get("/api/admin/resources")
def admin_resources(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    tool_res = [f"tool:{t['function']['name']}" for t in tools.TOOL_SCHEMAS]
    return {"tools": tool_res, "mcps": mcp_hub.server_resources()}


@app.get("/api/admin/users")
def admin_users(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    users = auth.list_users()
    for u in users:
        try:
            u["voice_samples"] = biometrics.count_for_user(u["id"])
        except Exception:
            u["voice_samples"] = 0
    return {"users": users}


@app.post("/api/admin/users")
def admin_create_user(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    u = (body or {}).get("username", "").strip()
    p = (body or {}).get("password") or None        # passwortlos erlaubt
    if not u:
        raise HTTPException(status_code=400, detail="Benutzername nötig.")
    try:
        uid = auth.create_user(u, p, body.get("group_ids", []))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Anlegen fehlgeschlagen: {e}")
    return {"id": uid}


@app.post("/api/admin/users/delete")
def admin_delete_user(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    auth.delete_user(int(body["id"]))
    return {"ok": True}


@app.post("/api/admin/users/groups")
def admin_user_groups(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    auth.set_user_groups(int(body["id"]), [int(g) for g in body.get("group_ids", [])])
    return {"ok": True}


@app.post("/api/admin/users/reset-password")
def admin_reset_pw(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    auth.admin_reset_password(int(body["id"]), body.get("password", ""))
    return {"ok": True}


@app.get("/api/admin/groups")
def admin_groups(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    return {"groups": auth.list_groups()}


@app.post("/api/admin/groups")
def admin_create_group(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    name = (body or {}).get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Gruppenname nötig.")
    return {"id": auth.create_group(name, bool(body.get("is_admin", False)))}


@app.post("/api/admin/groups/delete")
def admin_delete_group(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    auth.delete_group(int(body["id"]))
    return {"ok": True}


@app.post("/api/admin/groups/permissions")
def admin_group_perms(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    auth.set_group_permissions(int(body["id"]), body.get("resources", []))
    return {"ok": True}


@app.get("/api/admin/mcp")
def admin_mcp_list(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    return {"servers": mcp_hub.list_servers()}


@app.post("/api/admin/mcp")
async def admin_mcp_add(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    name = (body or {}).get("name", "").strip()
    url = (body or {}).get("url", "").strip()
    if not mcp_hub.valid_name(name):
        raise HTTPException(status_code=400, detail="Name nur Buchstaben/Ziffern/_ (max 40).")
    if not url:
        raise HTTPException(status_code=400, detail="URL nötig.")
    try:
        await asyncio.to_thread(mcp_hub.add_server, name, url)
        await mcp_hub.refresh(name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Hinzufügen fehlgeschlagen (Name vergeben?): {e}")
    return {"ok": True, "servers": mcp_hub.list_servers()}


@app.post("/api/admin/mcp/delete")
def admin_mcp_delete(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    mcp_hub.remove_server((body or {}).get("name", ""))
    return {"ok": True}


@app.post("/api/admin/mcp/refresh")
async def admin_mcp_refresh(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    await mcp_hub.refresh()
    return {"servers": mcp_hub.list_servers()}


@app.get("/api/admin/debug")
def admin_debug_get(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    return {"enabled": debug.enabled, "events": debug.events()}


@app.post("/api/admin/debug")
def admin_debug_toggle(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    en = bool((body or {}).get("enabled"))
    debug.set_enabled(en)
    config.update({"debug_enabled": en})       # über Neustart hinweg merken
    return {"enabled": debug.enabled}


@app.post("/api/admin/debug/clear")
def admin_debug_clear(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    debug.clear()
    return {"ok": True}


@app.get("/api/admin/devices")
def admin_devices(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    return {"devices": hub.devices()}


@app.post("/api/admin/devices/control")
async def admin_device_control(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    """Lautstärke (%) und/oder Mikrofon-Gain (dB) eines Geräts remote setzen.
    Das Gerät cappt die Lautstärke selbst (Verstärkerschutz)."""
    _admin(jarvis_admin_token)
    sid = (body or {}).get("session_id", "")
    if not sid or not hub.is_connected(sid):
        raise HTTPException(status_code=404, detail="Gerät nicht verbunden.")
    sent = {}
    if (body or {}).get("volume") is not None:
        pct = max(0, min(100, int(body["volume"])))
        await hub.push(sid, {"type": "set_volume", "percent": pct})
        sent["volume"] = pct
    if (body or {}).get("mic_gain") is not None:
        db = max(0.0, min(42.0, float(body["mic_gain"])))
        await hub.push(sid, {"type": "set_mic_gain", "db": db})
        sent["mic_gain"] = db
    if not sent:
        raise HTTPException(status_code=400, detail="Nichts zu setzen (volume und/oder mic_gain angeben).")
    return {"ok": True, "sent": sent}


# ── Autonomie: Automatisierungen + Blacklist ───────────────────────────────────
def _automation_view(a: dict) -> dict:
    return {**{k: a[k] for k in ("id", "title", "task", "trigger", "owner_user_id",
                                 "target_session", "enabled", "last_run", "last_result", "run_count", "next_run")},
            "kind": a.get("kind", "agent"),
            "fail_count": a.get("fail_count", 0),
            "state": a.get("state") or {},
            "trigger_text": automations.trigger_summary(a["trigger"]),
            "next_run_text": (time.strftime("%d.%m.%Y %H:%M", time.localtime(a["next_run"]))
                              if a.get("next_run") else None)}


@app.get("/api/admin/automations")
def admin_automations(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    return {"automations": [_automation_view(a) for a in automations.manager.list()],
            "scheduler": automations.manager.enabled}


@app.post("/api/admin/automations")
def admin_automation_create(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    b = body or {}
    if not b.get("task") or not b.get("trigger"):
        raise HTTPException(status_code=400, detail="task und trigger erforderlich.")
    a = automations.manager.create(
        title=b.get("title", ""), task=b["task"], trigger=b["trigger"],
        owner_user_id=b.get("owner_user_id"), target_session=b.get("target_session"))
    return _automation_view(a)


@app.post("/api/admin/automations/update")
def admin_automation_update(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    a = automations.manager.update((body or {}).get("id", ""), **{k: v for k, v in (body or {}).items() if k != "id"})
    if not a:
        raise HTTPException(status_code=404, detail="Automatisierung nicht gefunden.")
    return _automation_view(a)


@app.post("/api/admin/automations/delete")
def admin_automation_delete(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    return {"ok": automations.manager.delete((body or {}).get("id", ""))}


@app.post("/api/admin/automations/run")
async def admin_automation_run(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    return await automations.manager.run_now((body or {}).get("id", ""))


# ── Selbst-gebaute Skills (global; Admin kann editieren/deaktivieren/löschen) ──
@app.get("/api/admin/skills")
def admin_skills(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    return {"skills": skills.list_all()}


@app.post("/api/admin/skills/update")
async def admin_skill_update(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    b = body or {}
    s = skills.get(b.get("name", ""))
    if not s:
        raise HTTPException(status_code=404, detail="Unbekanntes Skill.")
    if b.get("code") is not None:                      # geänderter Code wird vor dem Speichern getestet
        test = await asyncio.to_thread(skills.run_skill_code, b["code"], b.get("test_args") or {},
                                       "skills", bool(b.get("net", s.get("net"))))
        if not test["ok"]:
            raise HTTPException(status_code=400, detail="Code-Test fehlgeschlagen: " + test["error"])
    fields = {k: b[k] for k in ("description", "code", "net", "enabled") if k in b}
    return skills.update(s["name"], **fields)


@app.post("/api/admin/skills/delete")
def admin_skill_delete(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    return {"ok": skills.delete((body or {}).get("name", ""))}


@app.post("/api/admin/skills/run")
async def admin_skill_run(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    b = body or {}
    s = skills.get(b.get("name", ""))
    if not s:
        raise HTTPException(status_code=404, detail="Unbekanntes Skill.")
    return await asyncio.to_thread(skills.run_skill_code, s["code"], b.get("args") or {},
                                   "skills", s.get("net", False))


@app.get("/api/admin/autonomy")
def admin_autonomy_get(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    cfg = config.get()
    return {
        "enabled": cfg.get("autonomous_enabled", True),
        "tool_blacklist": cfg.get("autonomous_tool_blacklist", []),
        "mcp_blacklist": cfg.get("autonomous_mcp_blacklist", []),
        "event_cooldown_s": cfg.get("autonomous_event_cooldown_s", 30),
        "tools": [t["function"]["name"] for t in tools.TOOL_SCHEMAS],
        "mcps": [r.split(":", 1)[1] for r in mcp_hub.server_resources()],
        "events": automations.known_events(),
    }


@app.post("/api/admin/autonomy")
def admin_autonomy_set(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    b = body or {}
    patch = {}
    if "enabled" in b:
        patch["autonomous_enabled"] = bool(b["enabled"])
    if "tool_blacklist" in b:
        patch["autonomous_tool_blacklist"] = list(b["tool_blacklist"])
    if "mcp_blacklist" in b:
        patch["autonomous_mcp_blacklist"] = list(b["mcp_blacklist"])
    if "event_cooldown_s" in b:
        patch["autonomous_event_cooldown_s"] = int(b["event_cooldown_s"])
    cfg = config.update(patch)
    automations.manager.enabled = bool(cfg.get("autonomous_enabled", True))
    automations.manager.cooldown_s = int(cfg.get("autonomous_event_cooldown_s", 30))
    return {"ok": True, "enabled": automations.manager.enabled}


@app.post("/api/admin/events/fire")
async def admin_event_fire(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    """Ereignis manuell auslösen (Test / Integration externer Quellen wie Smarthome/MCP-Push)."""
    _admin(jarvis_admin_token)
    name = (body or {}).get("event", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="event erforderlich.")
    fired = await automations.manager.dispatch_event(name, (body or {}).get("payload") or {})
    return {"fired": fired}


@app.get("/api/admin/messaging")
def admin_messaging_get(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    cfg = config.get()
    tok = cfg.get("telegram_bot_token", "")
    return {
        "enabled": cfg.get("telegram_enabled", False),
        "has_token": bool(tok),
        "token_hint": ("…" + tok[-4:]) if tok else "",
        "default_chat_id": cfg.get("telegram_default_chat_id", ""),
        "bot": messaging.bot_info() if messaging.enabled() else None,
    }


@app.post("/api/admin/messaging")
def admin_messaging_set(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    b = body or {}
    patch = {}
    if "enabled" in b:
        patch["telegram_enabled"] = bool(b["enabled"])
    if b.get("bot_token"):                                # nur setzen, wenn neu eingegeben
        patch["telegram_bot_token"] = str(b["bot_token"]).strip()
    if "default_chat_id" in b:
        patch["telegram_default_chat_id"] = str(b["default_chat_id"]).strip()
    config.update(patch)
    return {"ok": True, "enabled": messaging.enabled()}


@app.post("/api/admin/messaging/test")
async def admin_messaging_test(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    chat = str((body or {}).get("chat_id") or config.get().get("telegram_default_chat_id", "")).strip()
    if not chat:
        raise HTTPException(status_code=400, detail="Keine Chat-ID angegeben.")
    ok = await asyncio.to_thread(messaging.send_to_chat, chat, "✅ JARVIS-Testnachricht — der Kanal funktioniert.")
    return {"ok": ok}


@app.post("/api/admin/users/telegram")
def admin_user_telegram(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    chat = str(body.get("chat_id", "")).strip()
    auth.set_telegram_chat(int(body["id"]), chat)
    if chat:
        messaging.clear_pending(chat)                 # zugeordnet → nicht mehr „ausstehend"
    return {"ok": True}


@app.get("/api/admin/messaging/pending")
def admin_messaging_pending(jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    return {"pending": messaging.pending()}


@app.post("/api/admin/messaging/pending/clear")
def admin_messaging_pending_clear(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    messaging.clear_pending((body or {}).get("chat_id"))
    return {"ok": True}


@app.get("/admin")
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


# ── Downloads (Client-/Satelliten-Pakete) ─────────────────────────────────────
_SAT_DIR = BASE_DIR.parent / "deploy" / "satellite"
_SAT_FILES = ["satellite.py", "config.example", "install.sh", "jarvis-satellite.service", "README.md"]


_ESP_DIR = BASE_DIR.parent / "deploy" / "satellite-esp"


def _build_satellite_tar() -> bytes:
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in _SAT_FILES:
            p = _SAT_DIR / name
            if p.exists():
                tar.add(p, arcname=f"satellite/{name}")
    return buf.getvalue()


def _build_dir_tar(path: Path, arc: str) -> bytes:
    import io
    import tarfile
    # Build-Artefakte/Caches nie mitliefern (sonst Hunderte MB; managed_components wird neu geladen).
    _skip = {"__pycache__", "build", "managed_components"}
    _skip_files = {"dependencies.lock", "build_log.txt", "sdkconfig", "sdkconfig.old"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(path.rglob("*")):
            if not p.is_file():
                continue
            if _skip & set(p.relative_to(path).parts) or p.name in _skip_files:
                continue
            tar.add(p, arcname=f"{arc}/{p.relative_to(path)}")
    return buf.getvalue()


@app.get("/api/downloads")
def downloads_list():
    sat_ready = all((_SAT_DIR / n).exists() for n in ("satellite.py", "install.sh"))
    return {"items": [
        {"id": "satellite-pi", "name": "Raspberry-Pi-Satellit",
         "desc": "Sprachsatellit für Pi 3B+/Pi 2 (Wake-Word 'Jarvis', openWakeWord). Tarball + install.sh.",
         "available": sat_ready, "url": "/api/download/satellite-pi", "filename": "jarvis-satellite-pi.tar.gz"},
        {"id": "satellite-esp", "name": "ESP32-S3 Firmware (Waveshare, Quellcode)",
         "desc": "ESP-IDF-Firmware für das Waveshare ESP32-S3-AUDIO-Board (Wake-Word 'Jarvis'). "
                 "Quellprojekt zum Bauen mit idf.py.",
         "available": (_ESP_DIR / "main" / "jarvis_satellite.c").exists(),
         "url": "/api/download/satellite-esp", "filename": "jarvis-satellite-esp.tar.gz"},
        *_client_items(),
    ]}


# ── Desktop-Client-Pakete (Tauri-Builds je OS) ────────────────────────────────
_CLIENT_DIST = BASE_DIR.parent / "deploy" / "desktop" / "dist"
_CLIENT_PLATFORMS = {
    "linux":   {"name": "Desktop-Client · Linux (.deb)",   "globs": ["*.deb"]},
    "windows": {"name": "Desktop-Client · Windows (.msi/.exe)", "globs": ["*.msi", "*-setup.exe", "*.exe"]},
    "macos":   {"name": "Desktop-Client · macOS (.dmg)",   "globs": ["*.dmg"]},
}


def _client_file(platform: str) -> Path | None:
    info = _CLIENT_PLATFORMS.get(platform)
    if not info or not _CLIENT_DIST.exists():
        return None
    hits = [p for g in info["globs"] for p in _CLIENT_DIST.glob(g) if p.is_file()]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def _client_items() -> list[dict]:
    items = []
    for plat, info in _CLIENT_PLATFORMS.items():
        f = _client_file(plat)
        items.append({
            "id": f"client-{plat}", "name": info["name"],
            "desc": ("OS-Agent (Tray + Berechtigungs-UI). " +
                     (f"Bereit: {f.name}" if f else "Noch nicht gebaut/hochgeladen.")),
            "available": f is not None,
            "url": f"/api/download/client/{plat}" if f else None,
            "filename": f.name if f else None,
        })
    return items


@app.get("/api/download/client/{platform}")
def download_client(platform: str):
    f = _client_file(platform)
    if not f:
        raise HTTPException(status_code=404, detail="Kein Paket für diese Plattform vorhanden.")
    return FileResponse(str(f), filename=f.name, media_type="application/octet-stream")


@app.post("/api/admin/client-upload")
async def admin_client_upload(file: UploadFile = File(...), platform: str = Form(...),
                              jarvis_admin_token: str | None = Cookie(default=None)):
    """Ein auf dem jeweiligen OS gebautes Client-Paket (z.B. Windows-.msi) hochladen →
    erscheint sofort im Download-Bereich."""
    _admin(jarvis_admin_token)
    if platform not in _CLIENT_PLATFORMS:
        raise HTTPException(status_code=400, detail="Unbekannte Plattform.")
    name = Path(file.filename or "").name
    allowed = {"linux": (".deb",), "windows": (".msi", ".exe"), "macos": (".dmg",)}[platform]
    if not name.lower().endswith(allowed):
        raise HTTPException(status_code=400, detail=f"Für {platform} sind {allowed} erwartet.")
    _CLIENT_DIST.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    (_CLIENT_DIST / name).write_bytes(data)
    return {"ok": True, "platform": platform, "filename": name, "bytes": len(data)}


@app.get("/api/download/satellite-pi")
def download_satellite_pi():
    if not (_SAT_DIR / "satellite.py").exists():
        raise HTTPException(status_code=404, detail="Satelliten-Paket nicht gefunden.")
    data = _build_satellite_tar()
    return Response(content=data, media_type="application/gzip",
                    headers={"Content-Disposition": "attachment; filename=jarvis-satellite-pi.tar.gz"})


@app.get("/api/download/satellite-esp")
def download_satellite_esp():
    if not (_ESP_DIR / "main" / "jarvis_satellite.c").exists():
        raise HTTPException(status_code=404, detail="ESP-Firmware-Paket nicht gefunden.")
    data = _build_dir_tar(_ESP_DIR, "jarvis-satellite-esp")
    return Response(content=data, media_type="application/gzip",
                    headers={"Content-Disposition": "attachment; filename=jarvis-satellite-esp.tar.gz"})


@app.get("/downloads")
def downloads_page():
    return FileResponse(STATIC_DIR / "downloads.html")


@app.on_event("startup")
async def _startup():
    # Vektor-Store + Auth initialisieren — nicht-blockierend bei DB-Ausfall
    try:
        await asyncio.to_thread(store.init)
        await asyncio.to_thread(auth.init)
        await asyncio.to_thread(biometrics.init)
        await asyncio.to_thread(mcp_hub.init)
        await mcp_hub.refresh()       # MCP-Tool-Listen laden
        await asyncio.to_thread(services._get_embed_model)   # lokales Embedding vorladen
        debug.set_enabled(bool(config.get().get("debug_enabled", False)))
    except Exception as e:
        print(f"[startup] DB/MCP-Init: {e}")

    async def _on_fire(info: dict):
        # Timer/Wecker sind nur EIN Nutzer des universellen Rückkanals; Automatisierungen,
        # Cronjobs, Smarthome-Events usw. rufen `announce(...)` genauso auf.
        msg = f"Dein Timer „{info['label']}“ ist abgelaufen."
        await announce(info["session_id"], msg, kind="timer_alarm", label=info["label"])
        uid = (hub.get_identity(info["session_id"]) or {}).get("user_id")
        if messaging.enabled() and uid is not None:
            await asyncio.to_thread(messaging.send_to_user, uid, f"⏰ {msg}")
        automations.emit("timer_elapsed", {"label": info["label"], "session_id": info["session_id"]})
    timers.manager.on_fire = _on_fire

    # Autonomie-Scheduler starten (geplante + ereignisgesteuerte Selbstläufe)
    _acfg = config.get()
    automations.manager.runner = _run_automation
    automations.manager.deliver = _deliver_automation
    automations.manager.enabled = bool(_acfg.get("autonomous_enabled", True))
    automations.manager.cooldown_s = int(_acfg.get("autonomous_event_cooldown_s", 30))
    automations.manager.start()

    # Eingehender Messaging-Kanal (Telegram) — idlet, solange deaktiviert
    asyncio.create_task(_telegram_poller())


@app.post("/api/stt")
async def stt(file: UploadFile = File(...), session_id: str = Form(default="")):
    cfg = config.get()
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="Leere Audiodatei.")
    try:
        text = services.transcribe(audio, file.filename or "audio.webm", cfg)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"STT-Fehler: {e}")

    # Sprecher-Erkennung aus DEMSELBEN Audio → Session-Identität (nicht client-fälschbar).
    # Embedding wird gepuffert, damit es beim Onboarding einem Nutzer zugeordnet werden kann.
    speaker = None
    try:
        thr = float(cfg.get("voice_id_threshold", 0.75))
        emb = await asyncio.to_thread(biometrics.embed_audio, audio, file.filename or "audio.webm")
        speaker = await asyncio.to_thread(biometrics.identify_by_embedding, emb, thr)
        if session_id:
            hub.set_last_voice(session_id, emb)
    except Exception as e:
        print(f"[stt] Sprecher-Erkennung übersprungen: {e}")
    if session_id:
        hub.set_identity(session_id, speaker)
    if speaker and session_id:
        asyncio.create_task(automations.manager.dispatch_event(
            "speaker_recognized",
            {"username": speaker.get("username"), "user_id": speaker.get("user_id"), "session_id": session_id}))
    debug.log("stt", session=session_id, transcript=text,
              speaker=(speaker or {}).get("username"), confidence=(speaker or {}).get("confidence"))
    return {"text": text, "speaker": speaker}


@app.post("/api/admin/users/enroll-voice")
async def admin_enroll_voice(file: UploadFile = File(...), user_id: int = Form(...),
                             jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="Leere Aufnahme.")
    try:
        await asyncio.to_thread(biometrics.enroll, int(user_id), audio, file.filename or "audio.webm")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Enrollment fehlgeschlagen: {e}")
    return {"user_id": user_id, "samples": biometrics.count_for_user(int(user_id))}


@app.post("/api/admin/users/clear-voice")
def admin_clear_voice(body: dict, jarvis_admin_token: str | None = Cookie(default=None)):
    _admin(jarvis_admin_token)
    return {"removed": biometrics.clear_user(int(body["id"]))}


@app.post("/api/vision")
async def vision(file: UploadFile = File(...), question: str = Form(default=""),
                 session_id: str = Form(default="")):
    """Bild-Upload (Browser) → multimodale Analyse. Ergebnis wird auch in den
    Gesprächsverlauf der Session aufgenommen, damit Folgefragen Kontext haben."""
    cfg = config.get()
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Leeres Bild.")
    import base64
    mime = file.content_type or "image/jpeg"
    data_url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    try:
        answer = await asyncio.to_thread(services.vision_call, question, data_url, cfg)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Bildanalyse fehlgeschlagen: {e}")
    if session_id:
        hub.append_history(session_id, "user", f"[Bild gesendet] {question}".strip())
        hub.append_history(session_id, "assistant", answer)
    debug.log("vision", session=session_id, question=question[:120], bytes=len(data))
    return {"answer": answer}


@app.post("/api/tts")
def tts(req: TTSRequest):
    cfg = config.get()
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Kein Text.")
    try:
        audio, media_type = services.synthesize(req.text, cfg)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TTS-Fehler: {e}")
    return Response(content=audio, media_type=media_type)


# ── Web-UI (statisch) ─────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
