"""Clients für den GPU-Inferenz-Tier (Tier 2).

Drei dünne HTTP-Clients gegen die OpenAI-kompatiblen Dienste auf dem GPU-Server:
  • LLM  — llama.cpp / llama-swap  (/v1/chat/completions)
  • STT  — faster-whisper-server   (/v1/audio/transcriptions)
  • TTS  — deutscher Kokoro        (/v1/audio/speech)

Endpoints/Modelle kommen aus config.py, damit sie später über das UI
umgestellt werden können.
"""
from __future__ import annotations

import io
import json
import re
import threading
import wave
from pathlib import Path

import requests

_PIPER_DIR = Path(__file__).resolve().parent / "piper_voices"
_PIPER_CACHE: dict = {}          # voice-name → geladene PiperVoice (einmalig laden)
_PIPER_LOCK = threading.Lock()


# ── LLM ─────────────────────────────────────────────────────────────────────

def chat(messages: list[dict], cfg: dict) -> str:
    """Eine nicht-streamende Chat-Antwort. Trennt `reasoning_content`
    (gemma4 ist ein Reasoning-Modell) vom eigentlichen, gesprochenen `content`.

    Timeout und Token-Budget kommen aus der Config (UI-einstellbar). Wenn das
    Modell das Budget komplett fürs Reasoning verbraucht (finish_reason=length,
    content leer), wird eine klare Fehlermeldung geworfen statt einer Leerantwort.
    """
    url     = cfg["llm_url"].rstrip("/")
    timeout = int(cfg.get("llm_timeout", 180))
    payload = {
        "model":      cfg["llm_model"],
        "messages":   messages,
        "stream":     False,
        "max_tokens": int(cfg.get("llm_max_tokens", 1024)),
    }
    resp = requests.post(f"{url}/v1/chat/completions", json=payload, timeout=timeout)
    resp.raise_for_status()
    choice  = resp.json()["choices"][0]
    msg     = choice.get("message", {})
    content = (msg.get("content") or "").strip()
    if not content and choice.get("finish_reason") == "length":
        raise RuntimeError(
            "Antwort abgeschnitten: das Reasoning-Modell hat das Token-Budget "
            f"({payload['max_tokens']}) komplett fürs Nachdenken verbraucht. "
            "Erhöhe 'llm_max_tokens' in den Einstellungen oder nutze ein "
            "Nicht-Reasoning-Modell (z.B. qwen2.5-7b)."
        )
    return content


def vision_call(prompt: str, image_url: str, cfg: dict, timeout: int = 150) -> str:
    """Bildanalyse über das multimodale Modell (vision_model). `image_url` darf eine
    öffentliche http(s)-URL ODER eine data:-URI (base64) sein. Gibt deutschen Text zurück."""
    url   = cfg["llm_url"].rstrip("/")
    model = cfg.get("vision_model") or cfg["llm_model"]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt or "Beschreibe dieses Bild auf Deutsch."},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]}],
        "stream": False,
        "max_tokens": int(cfg.get("llm_max_tokens", 1024)),
    }
    resp = requests.post(f"{url}/v1/chat/completions", json=payload, timeout=timeout)
    resp.raise_for_status()
    msg = resp.json()["choices"][0].get("message", {}) or {}
    return (msg.get("content") or "").strip()


def _thinking_kwargs(cfg: dict, think: bool) -> dict:
    """chat_template_kwargs für gemma4: Denken aus (schnell) oder an (mit Budget-Cap).
    Wird pro Request gesetzt — Standard aus, nur für komplexe Flows an."""
    if think:
        return {"enable_thinking": True, "max_thinking_tokens": int(cfg.get("thinking_budget", 512))}
    return {"enable_thinking": False}


def llm_call(messages: list[dict], cfg: dict, tools: list | None = None, think: bool = False) -> dict:
    """Chat-Aufruf mit optionalem Tool-Calling. Gibt
    {content, tool_calls:[{id,name,args}], raw, finish} zurück.
    `think` schaltet gemma4s Reasoning an (für mehrstufige/identitätsbezogene Flows)."""
    url     = cfg["llm_url"].rstrip("/")
    timeout = int(cfg.get("llm_timeout", 180))
    payload: dict = {
        "model":      cfg["llm_model"],
        "messages":   messages,
        "stream":     False,
        "max_tokens": int(cfg.get("llm_max_tokens", 1024)),
        "chat_template_kwargs": _thinking_kwargs(cfg, think),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    resp = requests.post(f"{url}/v1/chat/completions", json=payload, timeout=timeout)
    resp.raise_for_status()
    choice  = resp.json()["choices"][0]
    msg     = choice.get("message", {}) or {}
    content = (msg.get("content") or "").strip()

    parsed: list[dict] = []
    raw_calls: list[dict] = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        fn   = tc.get("function", {}) or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {}
            args_str = raw_args
        else:
            args = raw_args or {}
            args_str = json.dumps(args, ensure_ascii=False)
        tid = tc.get("id") or f"call_{i}"
        parsed.append({"id": tid, "name": fn.get("name", ""), "args": args})
        raw_calls.append({"id": tid, "type": "function",
                          "function": {"name": fn.get("name", ""), "arguments": args_str}})

    raw = {"role": "assistant", "content": content}
    if raw_calls:
        raw["tool_calls"] = raw_calls
    return {"content": content, "tool_calls": parsed, "raw": raw, "finish": choice.get("finish_reason")}


_embed_model = None
_embed_lock = threading.Lock()


def _get_embed_model():
    """Lokales nomic-Embedding (fastembed/ONNX, CPU) — einmalig geladen & gecacht.
    Vermeidet den teuren llama-swap-Modellwechsel pro Turn (Embeddings liefen vorher
    über llama.cpp und verdrängten dort das Chat-Modell)."""
    global _embed_model
    with _embed_lock:
        if _embed_model is None:
            from fastembed import TextEmbedding
            _embed_model = TextEmbedding(model_name="nomic-ai/nomic-embed-text-v1.5")
        return _embed_model


def embed(texts: list[str], cfg: dict | None = None, task: str = "search_document") -> list[list[float]]:
    """Lokale Embeddings (768-dim). nomic-Prefixe: 'search_document' für gespeicherte
    Texte, 'search_query' für Suchanfragen."""
    m = _get_embed_model()
    inputs = [f"{task}: {t}" for t in texts]
    return [list(map(float, e)) for e in m.embed(inputs)]


_SENT_END = re.compile(r"(?<=[.!?…])\s+|\n\n")


def llm_stream(messages: list[dict], cfg: dict, tools: list | None = None, think: bool = False):
    """Streamender Chat-Aufruf (llama.cpp SSE). Yields:
       {"type":"sentence","text":...}  — je vollständiger Satz (für sofortiges TTS)
       {"type":"done","content","tool_calls":[{id,name,args}],"raw"}  — am Ende
    `think` schaltet gemma4s Reasoning an. Reasoning-Tokens werden NICHT als Sätze gesendet."""
    url     = cfg["llm_url"].rstrip("/")
    timeout = int(cfg.get("llm_timeout", 180))
    payload: dict = {
        "model":      cfg["llm_model"],
        "messages":   messages,
        "stream":     True,
        "max_tokens": int(cfg.get("llm_max_tokens", 1024)),
        "chat_template_kwargs": _thinking_kwargs(cfg, think),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    full, buf = "", ""
    tc_frag: dict = {}
    with requests.post(f"{url}/v1/chat/completions", json=payload, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except Exception:
                continue
            choice = (chunk.get("choices") or [{}])[0]
            delta  = choice.get("delta", {}) or {}
            text   = delta.get("content") or ""
            if text:
                full += text
                buf  += text
                while True:
                    m = _SENT_END.search(buf)
                    if not m:
                        break
                    sent = buf[:m.start()].strip()
                    buf = buf[m.end():]
                    if sent:
                        yield {"type": "sentence", "text": sent}
            for tc in (delta.get("tool_calls") or []):
                idx = tc.get("index", 0)
                f = tc_frag.setdefault(idx, {"id": "", "name": "", "args": ""})
                f["id"] = f["id"] or tc.get("id", "")
                fn = tc.get("function", {}) or {}
                f["name"] += fn.get("name") or ""
                f["args"] += fn.get("arguments") or ""
    if buf.strip():
        yield {"type": "sentence", "text": buf.strip()}

    tool_calls, raw_calls = [], []
    for i in sorted(tc_frag):
        f = tc_frag[i]
        try:
            args = json.loads(f["args"]) if f["args"] else {}
        except Exception:
            args = {}
        tid = f["id"] or f"call_{i}"
        tool_calls.append({"id": tid, "name": f["name"], "args": args})
        raw_calls.append({"id": tid, "type": "function",
                          "function": {"name": f["name"], "arguments": f["args"] or "{}"}})
    raw = {"role": "assistant", "content": full}
    if raw_calls:
        raw["tool_calls"] = raw_calls
    yield {"type": "done", "content": full.strip(), "tool_calls": tool_calls, "raw": raw}


def list_models(cfg: dict, timeout: int = 8) -> list[str]:
    url = cfg["llm_url"].rstrip("/")
    resp = requests.get(f"{url}/v1/models", timeout=timeout)
    resp.raise_for_status()
    return [m.get("id", "") for m in resp.json().get("data", [])]


# ── STT ─────────────────────────────────────────────────────────────────────

def transcribe(audio_bytes: bytes, filename: str, cfg: dict, timeout: int = 120) -> str:
    url   = cfg["stt_url"].rstrip("/")
    files = {"file": (filename or "audio.webm", audio_bytes, "application/octet-stream")}
    data  = {"model": cfg["stt_model"], "response_format": "json"}
    lang  = cfg.get("stt_language")
    if lang and lang.lower() != "auto":
        data["language"] = lang
    resp = requests.post(f"{url}/v1/audio/transcriptions", files=files, data=data, timeout=timeout)
    resp.raise_for_status()
    try:
        return (resp.json().get("text") or "").strip()
    except ValueError:
        return resp.text.strip()


# ── TTS ─────────────────────────────────────────────────────────────────────

def synthesize(text: str, cfg: dict, timeout: int = 120) -> tuple[bytes, str]:
    """Synthese je nach `tts_engine`. Gibt (audio_bytes, media_type) zurück.

      • "edge"   — Microsoft EdgeTTS (Cloud, ~0.6 s, natürliche DE-Stimmen).
                   Stimme z.B. de-DE-ConradNeural (m) / de-DE-KatjaNeural (w).
      • "piper"  — Piper (lokal, CPU, offline, ~0.3 s). Stimme z.B. de_DE-thorsten-medium.
      • "kokoro" — deutscher Kokoro-Container auf dem GPU-Server (offline, langsamer).
    """
    engine = cfg.get("tts_engine", "edge").lower()
    if engine == "edge":
        return _synth_edge(cfg.get("tts_voice_edge", "de-DE-ConradNeural"), text), "audio/mpeg"
    if engine == "piper":
        return _synth_piper(cfg.get("tts_voice_piper", "de_DE-thorsten-medium"), text), "audio/wav"
    # kokoro / ttsserver
    url     = cfg["tts_url"].rstrip("/")
    payload = {
        "model":           cfg.get("tts_model", "kokoro"),
        "voice":           cfg.get("tts_voice_kokoro", "martin"),
        "input":           text,
        "response_format": "wav",
    }
    resp = requests.post(f"{url}/v1/audio/speech", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.content, "audio/wav"


def _synth_piper(voice_name: str, text: str) -> bytes:
    """Lokale Piper-Synthese (CPU, offline). Lädt die Stimme einmalig und cacht sie;
    fehlende Stimmen werden beim ersten Mal automatisch heruntergeladen."""
    voice = _get_piper_voice(voice_name)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        voice.synthesize_wav(text, wf)
    return buf.getvalue()


def _get_piper_voice(voice_name: str):
    with _PIPER_LOCK:
        if voice_name in _PIPER_CACHE:
            return _PIPER_CACHE[voice_name]
        from piper import PiperVoice
        model = _PIPER_DIR / f"{voice_name}.onnx"
        if not model.exists():
            _download_piper_voice(voice_name)
        voice = PiperVoice.load(str(model))
        _PIPER_CACHE[voice_name] = voice
        return voice


def _download_piper_voice(voice_name: str) -> None:
    import subprocess
    import sys
    _PIPER_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "piper.download_voices",
         "--download-dir", str(_PIPER_DIR), voice_name],
        check=True, capture_output=True,
    )


def _synth_edge(voice: str, text: str) -> bytes:
    """EdgeTTS-Synthese (Cloud). Läuft im Threadpool-Worker von FastAPI,
    daher ist asyncio.run hier unbedenklich."""
    import asyncio
    import edge_tts

    async def _run() -> bytes:
        comm = edge_tts.Communicate(text, voice or "de-DE-ConradNeural")
        buf = bytearray()
        async for ch in comm.stream():
            if ch["type"] == "audio":
                buf.extend(ch["data"])
        return bytes(buf)

    return asyncio.run(_run())


# ── Health ──────────────────────────────────────────────────────────────────

def health(cfg: dict) -> dict:
    """Schneller Erreichbarkeits-Check aller drei Dienste."""
    out: dict = {}
    checks = {
        "llm": (cfg["llm_url"].rstrip("/") + "/v1/models"),
        "stt": (cfg["stt_url"].rstrip("/") + "/v1/models"),
        "tts": (cfg["tts_url"].rstrip("/") + "/v1/audio/voices"),
    }
    for name, u in checks.items():
        try:
            r = requests.get(u, timeout=4)
            out[name] = {"ok": r.status_code == 200, "status": r.status_code}
        except Exception as e:
            out[name] = {"ok": False, "error": str(e)[:120]}
    return out
