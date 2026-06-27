"""
JARVIS Plugin-Gateway — versionierte, authentifizierte Fassade /api/v1/* vor den
Core-Subsystemen (LLM, Vision, STT/TTS, RAG, Storage, Notify, Tools, Events, Scheduler).

Auth: Bearer-API-Key (plugins_registry.verify_key). Autorisierung = Key-Scopes UND
Nutzerrechte (auth.is_tool_allowed). Vollständige Spezifikation: ../pluginsystem.md.

app.py injiziert über set_hooks() die Funktionen, die im app-Modul leben
(announce, run_agent, invoke_tool, available_tool_schemas) sowie den session_hub.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

from fastapi import APIRouter, Header, Request, UploadFile, File, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse, JSONResponse

import config
import services
import store
import auth
import messaging
import debug
import plugins_registry as registry
import plugin_bus

router = APIRouter(prefix="/api/v1", tags=["plugin-api"])

# ── Hooks (von app.py gesetzt, um Zirkular-Importe zu vermeiden) ──────────────
_hooks: dict = {
    "announce": None,           # async announce(session_id, text, *, kind, **meta) -> dict
    "run_agent": None,          # async run_agent(task, user_id, allow_tools) -> str
    "invoke_tool": None,        # async invoke_tool(name, args, user_id) -> str
    "tool_schemas_for": None,   # tool_schemas_for(user_id) -> list[dict]
    "schedule_job": None,       # schedule_job(spec) -> dict
    "list_jobs": None, "delete_job": None,
    "hub": None,
}


def set_hooks(**kw) -> None:
    _hooks.update({k: v for k, v in kw.items() if v is not None})


# ── Auth-Kontext ──────────────────────────────────────────────────────────────

class PluginCtx:
    def __init__(self, key: dict, user_id, username: str | None):
        self.plugin_id = key["plugin_id"]
        self.scopes = key["scopes"]
        self.kid = key["kid"]
        self.user_id = user_id
        self.username = username

    def ns(self, scope: str = "user") -> str:
        return registry.kv_ns(self.plugin_id, self.user_id, scope)

    def rag_ns(self, collection: str) -> str:
        return f"plugin:{self.plugin_id}:u{self.user_id if self.user_id is not None else 'guest'}:{collection}"


def _err(status: int, code: str, message: str):
    return JSONResponse(status_code=status,
                        content={"error": {"code": code, "message": message, "status": status}})


async def _resolve(authorization: str | None, x_user: str | None) -> PluginCtx:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    key = await asyncio.to_thread(registry.verify_key, token)
    if not key:
        raise HTTPException(status_code=401, detail="Ungültiger oder fehlender Plugin-API-Key.")
    # Nutzer bestimmen: X-JARVIS-User (nur mit api:act_as_user) > Key-Bindung > Gast
    user_id, username = None, None
    if x_user and registry.has_scope(key, "api:act_as_user"):
        u = await asyncio.to_thread(auth.user_by_name, x_user.strip())
        if not u:
            raise HTTPException(status_code=400, detail=f"Unbekannter Nutzer „{x_user}“.")
        user_id, username = u["id"], u["username"]
    elif key.get("user_binding") is not None:
        user_id = key["user_binding"]
        username = await asyncio.to_thread(auth.username_by_id, user_id)
    return PluginCtx(key, user_id, username)


def _need(ctx: PluginCtx, scope: str) -> None:
    if not registry.has_scope({"scopes": ctx.scopes}, scope):
        raise HTTPException(status_code=403, detail=f"Scope „{scope}“ fehlt für diesen API-Key.")


async def _need_tool(ctx: PluginCtx, resource: str) -> None:
    """tool:/mcp:-Ressourcen zusätzlich gegen die Nutzerrechte prüfen (Defense in Depth)."""
    ok = await asyncio.to_thread(auth.is_tool_allowed, ctx.user_id, resource)
    if not ok:
        raise HTTPException(status_code=403, detail=f"Nutzer hat keine Freigabe für „{resource}“.")


# Eine Dependency-freie Bequemlichkeit: jede Route ruft await _ctx(...) selbst.
async def _ctx(authorization, x_user):
    return await _resolve(authorization, x_user)


# ══════════════════════════════════════════════════════════════════════════════
# 1) Inference Hub — api:llm
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/inference/chat")
async def inference_chat(body: dict, authorization: str | None = Header(None),
                         x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:llm")
    cfg = config.get()
    sysp = (body.get("system_prompt") or cfg["system_prompt"]).strip()
    msgs = body.get("messages") or []
    if not msgs:
        raise HTTPException(status_code=400, detail="messages fehlt.")
    working = [{"role": "system", "content": sysp}] + [
        {"role": m.get("role", "user"), "content": m.get("content", "")} for m in msgs]
    think = bool(body.get("think", False))
    max_tokens = body.get("max_tokens")
    runcfg = dict(cfg)
    if max_tokens:
        runcfg["llm_max_tokens"] = int(max_tokens)
    if body.get("model"):
        runcfg["llm_model"] = body["model"]

    if body.get("stream"):
        def _sse(event, payload):
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        async def gen():
            loop = asyncio.get_running_loop()
            q: asyncio.Queue = asyncio.Queue()

            def produce():
                try:
                    for ev in services.llm_stream(working, runcfg, None, think):
                        loop.call_soon_threadsafe(q.put_nowait, ev)
                except Exception as e:
                    loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "detail": str(e)})
                loop.call_soon_threadsafe(q.put_nowait, None)
            import threading
            threading.Thread(target=produce, daemon=True).start()
            while True:
                ev = await q.get()
                if ev is None:
                    break
                if ev["type"] == "sentence":
                    yield _sse("sentence", {"text": ev["text"]})
                elif ev["type"] == "error":
                    yield _sse("error", {"detail": ev["detail"]}); return
                elif ev["type"] == "done":
                    yield _sse("done", {"content": ev.get("content", "")})
        return StreamingResponse(gen(), media_type="text/event-stream")

    reply = await asyncio.to_thread(services.chat, working, runcfg)
    debug.log("plugin_chat", plugin=ctx.plugin_id, user=ctx.username, chars=len(reply or ""))
    return {"reply": reply, "model": runcfg["llm_model"]}


@router.post("/inference/structure")
async def inference_structure(body: dict, authorization: str | None = Header(None),
                              x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:llm")
    text = (body.get("text") or "").strip()
    schema = body.get("schema") or {}
    if not text:
        raise HTTPException(status_code=400, detail="text fehlt.")
    cfg = config.get()
    sysp = ("Du extrahierst strukturierte Daten. Antworte AUSSCHLIESSLICH mit gültigem JSON, "
            "das EXAKT diesem JSON-Schema entspricht — keine Erklärung, kein Markdown, kein Codeblock.\n"
            "SCHEMA:\n" + json.dumps(schema, ensure_ascii=False))
    working = [{"role": "system", "content": sysp},
               {"role": "user", "content": text}]
    raw = await asyncio.to_thread(services.chat, working, cfg)
    parsed = _extract_json(raw)
    if parsed is None:
        # Ein Repair-Versuch
        working.append({"role": "assistant", "content": raw})
        working.append({"role": "user", "content": "Das war kein gültiges JSON. Gib NUR das JSON aus."})
        raw = await asyncio.to_thread(services.chat, working, cfg)
        parsed = _extract_json(raw)
    if parsed is None:
        return _err(502, "structure_failed", "Modell lieferte kein gültiges JSON.")
    return parsed


def _extract_json(text: str):
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    try:
        return json.loads(t)
    except Exception:
        pass
    # erste {...} oder [...] herausschneiden
    for op, cl in (("{", "}"), ("[", "]")):
        i, j = t.find(op), t.rfind(cl)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                continue
    return None


@router.post("/inference/agent")
async def inference_agent(body: dict, authorization: str | None = Header(None),
                          x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:llm")
    task = (body.get("task") or "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="task fehlt.")
    run_agent = _hooks.get("run_agent")
    if not run_agent:
        return _err(503, "unavailable", "Agenten-Loop nicht verfügbar.")
    allow = body.get("allow_tools")
    reply = await run_agent(task, ctx.user_id, allow)
    return {"reply": reply}


# ══════════════════════════════════════════════════════════════════════════════
# 2) Vision — api:vision
# ══════════════════════════════════════════════════════════════════════════════

def _image_data_url(body: dict) -> str:
    img = body.get("image") or ""
    if img.startswith("data:"):
        return img
    if img:                          # nackt base64 → als jpeg annehmen
        return f"data:image/jpeg;base64,{img}"
    raise HTTPException(status_code=400, detail="image (data-URL oder base64) fehlt.")


@router.post("/vision/analyze")
async def vision_analyze(body: dict, authorization: str | None = Header(None),
                         x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:vision")
    cfg = config.get()
    question = body.get("question") or "Beschreibe das Bild."
    data_url = _image_data_url(body)
    answer = await asyncio.to_thread(services.vision_call, question, data_url, cfg)
    return {"answer": answer, "model": cfg.get("vision_model")}


@router.post("/vision/ocr")
async def vision_ocr(body: dict, authorization: str | None = Header(None),
                     x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:vision")
    cfg = config.get()
    prompt = ("Lies ALLEN sichtbaren Text aus diesem Bild exakt aus und gib NUR den Text zurück, "
              "ohne Beschreibung.")
    answer = await asyncio.to_thread(services.vision_call, prompt, _image_data_url(body), cfg)
    return {"text": answer}


@router.post("/vision/classify")
async def vision_classify(body: dict, authorization: str | None = Header(None),
                          x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:vision")
    labels = body.get("labels") or []
    if not labels:
        raise HTTPException(status_code=400, detail="labels fehlt.")
    cfg = config.get()
    prompt = ("Ordne das Bild GENAU einem dieser Labels zu und antworte NUR mit dem Label-Wort: "
              + ", ".join(labels))
    ans = (await asyncio.to_thread(services.vision_call, prompt, _image_data_url(body), cfg)).strip()
    chosen = next((l for l in labels if l.lower() in ans.lower()), ans)
    return {"label": chosen, "raw": ans}


# ══════════════════════════════════════════════════════════════════════════════
# 3) Audio — api:stt / api:tts
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/audio/stt")
async def audio_stt(file: UploadFile = File(...), language: str = Form(default=""),
                    authorization: str | None = Header(None), x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:stt")
    cfg = config.get()
    if language:
        cfg = dict(cfg); cfg["stt_language"] = language
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Leere Audiodatei.")
    text = await asyncio.to_thread(services.transcribe, data, file.filename or "audio.webm", cfg)
    return {"text": text, "language": cfg.get("stt_language")}


@router.post("/audio/tts")
async def audio_tts(body: dict, request: Request, authorization: str | None = Header(None),
                    x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:tts")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text fehlt.")
    cfg = dict(config.get())
    engine = (body.get("voice") or body.get("engine") or "").lower()
    if engine in ("edge", "piper", "kokoro"):
        cfg["tts_engine"] = engine
    # Direkt auf einem Gerät ausspielen statt Bytes zurückzugeben?
    if body.get("deliver") == "announce" and body.get("session_id"):
        ann = _hooks.get("announce")
        if ann:
            res = await ann(body["session_id"], text, kind="tts")
            return {"delivered": res.get("delivered"), "spoken": res.get("spoken")}
    audio, media = await asyncio.to_thread(services.synthesize, text, cfg)
    return Response(content=audio, media_type=media)


# ══════════════════════════════════════════════════════════════════════════════
# 4) RAG / Gedächtnis — api:rag
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/rag/insert")
async def rag_insert(body: dict, authorization: str | None = Header(None),
                     x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:rag")
    coll = body.get("collection") or "default"
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content fehlt.")
    meta = body.get("metadata") or {}
    cfg = config.get()
    payload = content if not meta else content + "\n\n[meta] " + json.dumps(meta, ensure_ascii=False)
    emb = await asyncio.to_thread(services.embed, [payload], cfg, "search_document")
    src = meta.get("source") or f"{ctx.plugin_id}:{int(time.time())}"
    rid = await asyncio.to_thread(store.add, "plugin", payload, emb[0], ctx.rag_ns(coll), src)
    return {"id": rid, "chunks": 1}


@router.post("/rag/query")
async def rag_query(body: dict, authorization: str | None = Header(None),
                    x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:rag")
    coll = body.get("collection") or "default"
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query fehlt.")
    cfg = config.get()
    emb = await asyncio.to_thread(services.embed, [query], cfg, "search_query")
    k = int(body.get("limit", 5))
    min_score = float(body.get("min_score", 0.0))
    hits = await asyncio.to_thread(store.search, "plugin", emb[0], ctx.rag_ns(coll), k, min_score)
    results = [{"content": h["content"], "score": h["score"], "source": h["source"]} for h in hits]
    return {"results": results}


@router.post("/rag/ingest")
async def rag_ingest(body: dict, authorization: str | None = Header(None),
                     x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:rag")
    import knowledge
    coll = body.get("collection") or "default"
    text = body.get("text") or ""
    source = body.get("source") or f"{ctx.plugin_id}:{int(time.time())}"
    if not text.strip():
        raise HTTPException(status_code=400, detail="text fehlt.")
    cfg = config.get()
    # eigener kind 'plugin' über knowledge-Chunking
    chunks = knowledge._chunk(text)
    embs = await asyncio.to_thread(services.embed, chunks, cfg, "search_document")
    n = await asyncio.to_thread(store.add_many, "plugin", list(zip(chunks, embs)), ctx.rag_ns(coll), source)
    return {"chunks": n, "source": source}


@router.delete("/rag/source")
async def rag_delete(body: dict, authorization: str | None = Header(None),
                     x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:rag")
    coll = body.get("collection") or "default"
    source = body.get("source")
    if not source:
        raise HTTPException(status_code=400, detail="source fehlt.")
    n = await asyncio.to_thread(store.delete_source, source, ctx.rag_ns(coll))
    return {"deleted": n}


# ══════════════════════════════════════════════════════════════════════════════
# 5) Plugin-Storage (KV/Doc) — api:storage
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/storage/{collection}")
async def storage_list(collection: str, prefix: str = "", limit: int = 100, scope: str = "user",
                       authorization: str | None = Header(None), x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:storage")
    items = await asyncio.to_thread(registry.kv_list, ctx.plugin_id, ctx.ns(scope), collection, prefix, limit)
    return {"items": items}


@router.get("/storage/{collection}/{key}")
async def storage_get(collection: str, key: str, scope: str = "user",
                      authorization: str | None = Header(None), x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:storage")
    val = await asyncio.to_thread(registry.kv_get, ctx.plugin_id, ctx.ns(scope), collection, key)
    if val is None:
        return _err(404, "not_found", f"Kein Eintrag {collection}/{key}.")
    return {"key": key, "value": val}


@router.put("/storage/{collection}/{key}")
async def storage_put(collection: str, key: str, body: dict, scope: str = "user",
                      authorization: str | None = Header(None), x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:storage")
    value = body.get("value", body)         # akzeptiert {"value": …} ODER direkten Body
    await asyncio.to_thread(registry.kv_set, ctx.plugin_id, ctx.ns(scope), collection, key, value)
    return {"ok": True, "key": key}


@router.patch("/storage/{collection}/{key}")
async def storage_patch(collection: str, key: str, body: dict, scope: str = "user",
                        authorization: str | None = Header(None), x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:storage")
    new_val = await asyncio.to_thread(registry.kv_patch, ctx.plugin_id, ctx.ns(scope), collection, key, body)
    return {"ok": True, "key": key, "value": new_val}


@router.delete("/storage/{collection}/{key}")
async def storage_delete(collection: str, key: str, scope: str = "user",
                         authorization: str | None = Header(None), x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:storage")
    n = await asyncio.to_thread(registry.kv_delete, ctx.plugin_id, ctx.ns(scope), collection, key)
    return {"deleted": n}


# ══════════════════════════════════════════════════════════════════════════════
# 6) Channels / Notify — api:notify
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/channels/notify")
async def channels_notify(body: dict, authorization: str | None = Header(None),
                          x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:notify")
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text fehlt.")
    kind = body.get("kind") or "notify"
    speak = bool(body.get("speak", False))
    channels = body.get("channels") or ["auto"]
    meta = dict(body.get("meta") or {}); meta["plugin"] = ctx.plugin_id
    hub = _hooks.get("hub"); ann = _hooks.get("announce")
    targets, delivered, spoken = [], False, False

    # Zielsessions bestimmen
    sessions = []
    if body.get("session_id"):
        sessions = [body["session_id"]]
    elif hub and ctx.user_id is not None:
        sessions = hub.sessions_for_user(ctx.user_id)

    want_devices = "auto" in channels or "browser" in channels or "satellite" in channels
    if want_devices and ann:
        for sid in sessions:
            res = await ann(sid, text, kind=kind, speak=speak, **meta)
            delivered = delivered or res.get("delivered", False)
            spoken = spoken or res.get("spoken", False)
            targets.append(sid)

    # Telegram
    if ("auto" in channels or "telegram" in channels) and ctx.user_id is not None:
        if messaging.enabled():
            ok = await asyncio.to_thread(messaging.send_to_user, ctx.user_id, text)
            if ok:
                delivered = True; targets.append("telegram")

    debug.log("plugin_notify", plugin=ctx.plugin_id, user=ctx.username, notify_kind=kind, targets=targets)
    return {"delivered": delivered, "spoken": spoken, "targets": targets}


@router.post("/channels/photo")
async def channels_photo(body: dict, authorization: str | None = Header(None),
                         x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:notify")
    if ctx.user_id is None:
        raise HTTPException(status_code=400, detail="Nutzerkontext nötig (X-JARVIS-User oder Key-Bindung).")
    img_b64 = body.get("image") or ""
    if img_b64.startswith("data:"):
        img_b64 = img_b64.split(",", 1)[1]
    try:
        img = base64.b64decode(img_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="image (base64) ungültig.")
    ok = await asyncio.to_thread(messaging.send_photo_to_user, ctx.user_id, img, body.get("caption", ""))
    return {"delivered": bool(ok)}


# ══════════════════════════════════════════════════════════════════════════════
# 7) Tools-Bridge — tool:<name>
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/tools")
async def tools_list(authorization: str | None = Header(None), x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    fn = _hooks.get("tool_schemas_for")
    schemas = fn(ctx.user_id) if fn else []
    # nur Tools, deren Scope der Key trägt (oder '*')
    out = []
    for s in schemas:
        nm = s.get("function", {}).get("name", "")
        if registry.has_scope({"scopes": ctx.scopes}, f"tool:{nm}") or "*" in ctx.scopes:
            out.append(s)
    return {"tools": out}


@router.post("/tools/{name}/invoke")
async def tools_invoke(name: str, body: dict, authorization: str | None = Header(None),
                       x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    resource = f"mcp:{name.split('__')[1]}" if name.startswith("mcp__") else f"tool:{name}"
    _need(ctx, resource)
    await _need_tool(ctx, resource)
    fn = _hooks.get("invoke_tool")
    if not fn:
        return _err(503, "unavailable", "Tool-Bridge nicht verfügbar.")
    result = await fn(name, body.get("args") or {}, ctx.user_id)
    return {"result": result}


# ══════════════════════════════════════════════════════════════════════════════
# 8) Scheduler — api:scheduler
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/scheduler/jobs")
async def scheduler_create(body: dict, authorization: str | None = Header(None),
                           x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:scheduler")
    fn = _hooks.get("schedule_job")
    if not fn:
        return _err(503, "unavailable", "Scheduler nicht verfügbar.")
    spec = dict(body); spec["plugin_id"] = ctx.plugin_id; spec["owner_user_id"] = ctx.user_id
    job = await fn(spec)
    return job


@router.get("/scheduler/jobs")
async def scheduler_list(authorization: str | None = Header(None), x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:scheduler")
    fn = _hooks.get("list_jobs")
    return {"jobs": (fn(ctx.plugin_id) if fn else [])}


@router.delete("/scheduler/jobs/{job_id}")
async def scheduler_delete(job_id: str, authorization: str | None = Header(None),
                           x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:scheduler")
    fn = _hooks.get("delete_job")
    ok = fn(ctx.plugin_id, job_id) if fn else False
    return {"deleted": bool(ok)}


# ══════════════════════════════════════════════════════════════════════════════
# 9) Identität — /me, /users
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/me")
async def me(authorization: str | None = Header(None), x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    return {"plugin": ctx.plugin_id, "user": ctx.username, "user_id": ctx.user_id,
            "scopes": sorted(ctx.scopes)}


@router.get("/users")
async def users(authorization: str | None = Header(None), x_jarvis_user: str | None = Header(None)):
    ctx = await _resolve(authorization, x_jarvis_user)
    _need(ctx, "api:act_as_user")
    us = await asyncio.to_thread(auth.list_users)
    return {"users": [{"id": u["id"], "username": u["username"]} for u in us]}


@router.get("/health")
async def health_v1():
    return {"ok": True, "api": "v1", "bus": plugin_bus.stats()}


# ══════════════════════════════════════════════════════════════════════════════
# 10) Event-WebSocket — api:events
# ══════════════════════════════════════════════════════════════════════════════

@router.websocket("/ws")
async def plugin_ws(ws: WebSocket):
    token = ws.query_params.get("token")
    if not token:
        auth_h = ws.headers.get("authorization", "")
        if auth_h.lower().startswith("bearer "):
            token = auth_h[7:].strip()
    key = await asyncio.to_thread(registry.verify_key, token)
    if not key or not registry.has_scope(key, "api:events"):
        await ws.close(code=4401)
        return
    await ws.accept()
    sub_id, q = plugin_bus.subscribe([], plugin=key["plugin_id"])
    plugin_prefix = f"jarvis/plugin/{key['plugin_id']}/"

    async def pump():
        try:
            while True:
                event = await q.get()
                await ws.send_text(json.dumps(event, ensure_ascii=False))
        except Exception:
            pass

    pump_task = asyncio.create_task(pump())
    try:
        await ws.send_text(json.dumps({"op": "ready", "plugin": key["plugin_id"]}))
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            op = msg.get("op")
            if op == "subscribe":
                plugin_bus.add_topics(sub_id, msg.get("topics") or [])
            elif op == "unsubscribe":
                # neu setzen ohne die genannten
                pass
            elif op == "publish":
                topic = msg.get("topic") or ""
                # Plugins dürfen nur in ihren eigenen Namespace publizieren
                if not topic.startswith(plugin_prefix):
                    await ws.send_text(json.dumps({"op": "error",
                        "message": f"Nur Topics unter {plugin_prefix} erlaubt."}))
                    continue
                await plugin_bus.publish(topic, msg.get("payload") or {}, source=key["plugin_id"])
            elif op == "ping":
                await ws.send_text(json.dumps({"op": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        plugin_bus.unsubscribe(sub_id)
        pump_task.cancel()
