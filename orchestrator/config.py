"""Konfiguration des Orchestrators.

Lädt config.json, erlaubt Laufzeit-Updates (für die spätere UI-Einstellbarkeit
der Endpoints) und persistiert Änderungen wieder nach config.json.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
_LOCK = threading.Lock()

_DEFAULTS: dict = {
    "llm_url": "http://192.168.66.225:8080",
    "llm_model": "gemma4-12b",
    "vision_model": "gemma4-12b",
    "llm_max_tokens": 1024,
    "llm_timeout": 180,
    "llm_frequency_penalty": 0.3,   # gegen Wiederholungs-/Tool-Schleifen (0 = aus, 0.2–0.6 sinnvoll)
    "thinking_mode": "adaptive",  # adaptive = erst ohne Denken, bei Fehlschlag mit | auto | always | never
    "thinking_budget": 512,
    "debug_enabled": False,
    "voice_id_threshold": 0.65,
    "stt_url": "http://192.168.66.225:8001",
    "stt_model": "jimmymeister/whisper-large-v3-turbo-german-ct2",
    "stt_language": "de",
    "tts_engine": "edge",                      # edge | piper | kokoro
    "tts_voice_edge": "de-DE-ConradNeural",     # Cloud, natürlich
    "tts_voice_piper": "de_DE-thorsten-medium", # CPU, offline
    "tts_voice_kokoro": "martin",               # GPU-Container
    "tts_url": "http://192.168.66.225:8002",
    "tts_model": "kokoro",
    "system_prompt": "Du bist Jarvis, ein effizienter, professioneller deutschsprachiger Sprachassistent. Antworte IMMER auf Deutsch, kurz und direkt — höchstens 3 Sätze. Keine Floskeln, keine Rückfragen am Ende.",

    # ── Autonomie (geplante/ereignisgesteuerte Selbstläufe) ──────────────────────
    # Bei autonomen Läufen gelten die Rechte des Besitzers, ABER diese Tools/MCP-Server
    # sind zusätzlich gesperrt (Blacklist), egal welche Freigaben der Nutzer sonst hat.
    "autonomous_enabled": True,                 # Scheduler global an/aus
    "autonomous_tool_blacklist": [],            # z.B. ["create_user", "set_device_volume"]
    "autonomous_mcp_blacklist": [],             # z.B. ["smarthome"]  → kompletten Server sperren
    "autonomous_event_cooldown_s": 30,          # Ereignis-Trigger nicht öfter als alle N Sekunden

    # ── Code-Sandbox (Tier 3, eigener Container — niemals auf dem Host) ───────────
    "sandbox_url": "http://127.0.0.1:8090",
    "sandbox_priv_url": "http://127.0.0.1:8091",  # privilegierte Spur (Hostnetz+NET_RAW) für „erhöhte" Skills
    "sandbox_enabled": True,                    # Code-Ausführung global an/aus
    "sandbox_allow_network": True,              # Internet im Sandbox-Code (sonst unshare -rn)
    "sandbox_timeout_s": 30,                    # Job-Timeout (Sekunden)
    "fetch_allow_lan": False,                   # fetch_url darf interne LAN-Adressen laden (SSRF-Lockerung, Heimnetz)

    # ── Messaging-Kanal (Telegram) ───────────────────────────────────────────────
    "telegram_enabled": False,
    "telegram_bot_token": "",
    "telegram_default_chat_id": "",             # Fallback-Empfänger, wenn Nutzer keine eigene Chat-ID hat
}


def load() -> dict:
    cfg = dict(_DEFAULTS)
    try:
        cfg.update(json.loads(_CONFIG_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except Exception as e:  # pragma: no cover
        print(f"[config] Warnung: config.json nicht lesbar ({e}) — nutze Defaults.")
    return cfg


def get() -> dict:
    with _LOCK:
        return load()


def update(patch: dict) -> dict:
    """Merge *patch* in die Config und persistiere sie. Gibt die neue Config zurück."""
    with _LOCK:
        cfg = load()
        # Nur bekannte Schlüssel übernehmen
        for k, v in patch.items():
            if k in _DEFAULTS:
                cfg[k] = v
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
        return cfg
