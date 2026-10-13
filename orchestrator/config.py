"""Konfiguration des Orchestrators.

Lädt config.json, erlaubt Laufzeit-Updates (für die spätere UI-Einstellbarkeit
der Endpoints) und persistiert Änderungen wieder nach config.json.
"""
from __future__ import annotations

import json
import os
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
    "llm_ctx": 40960,               # Kontextfenster des Agenten-Modells (qwen3-14b: 40960) — für das Token-Budget (#3)
    "ctx_reserve_tokens": 2500,     # Reserve für System-Overhead + Sicherheitsabstand. Die Tool-Schemas
                                    # werden seit 2026-10-12 EXAKT mitgezählt (context_budget.fit
                                    # extra_tokens) und brauchen hier keine Pauschale mehr.
    # Ein einzelnes Werkzeug-Ergebnis hart kappen, bevor es in den Prompt geht: 12 000 Zeichen
    # ≈ 4 000 Tokens ≈ 7 s Prefill. Höher nur mit entsprechend größerem llm_ctx — vorher stand
    # hier 64 000 (≈ 21 000 Tokens) und sprengte das 16k-Fenster regelmäßig (HTTP 400).
    "tool_result_max_chars": 12000,
    "fetch_max_chars": 20000,       # Obergrenze dessen, was fetch_url überhaupt zurückgibt
    "profile_update_every": 6,      # Nutzerprofil höchstens alle N Turns neu generieren
    "chars_per_token": 3.0,         # grobe Token-Schätzung (deutsch ~3 Zeichen/Token)
    "summary_max_chars": 1500,      # Obergrenze der rollierenden Gesprächs-Zusammenfassung
    "llm_frequency_penalty": 0.3,   # gegen Wiederholungs-/Tool-Schleifen (0 = aus, 0.2–0.6 sinnvoll)
    # Embedding-Router (11.3): blendet nur passende Tool-Gruppen ein. Standard AUS — er wechselt die
    # Tool-Liste pro Thema, und die wird VOR den System-Text gerendert: der KV-Prefix-Cache des ganzen
    # Prompts ist damit jedes Mal hinüber (gemessen 16–24 s Prefill statt 0,4 s). Statt dessen: kleine,
    # stabile Kernliste (tools.CORE_TOOLS) + Nachladen per load_tools.
    "tool_router_enabled": False,
    "tool_router_threshold": 0.45,  # Mindest-Ähnlichkeit; darunter → volle Tool-Liste (konservativ)
    "llm_tool_temperature": 0.2,    # Temperatur NUR für Runden mit Tool-Angebot (stabilere Tool-Wahl;
                                    # -1 = Server-Default auch dort)
    "llm_cache_prompt": True,       # llama.cpp KV-Cache des stabilen Präfix (System-Prompt) wiederverwenden
    "llm_keepalive_s": 0,           # >0: Agenten-Modell alle N s warmhalten (1-Token-Ping), nur bei jüngster
                                    # Aktivität (<10 min) — verhindert llama-swap-Kaltstarts (502). 0 = aus,
                                    # damit andere Apps (z.B. Coding-Modell) ungestört swappen können.
    "smarthome_fast_confirm": True,  # Schalt-Bestätigung deterministisch statt extra LLM-Runde (~5 s schneller)
    # Eindeutige Messwert-Fragen („Wie ist der Stromverbrauch?", „Wie warm ist es im Wohnzimmer?")
    # direkt aus dem vorab aufgelösten Gerätewert beantworten, ohne LLM-Runde: Antwort in ~0,2 s
    # statt 3–150 s (je GPU-Last). Greift nur, wenn das beste Gerät frisch ist, die gefragte Größe
    # liefert und ein Wort des Nutzers im Namen trägt — sonst normal über das Modell.
    "smarthome_fast_read": True,
    "preresolve_agenda": True,       # Kalender-/Todo-Fragen: heutige Termine + offene To-dos vorab in den Prompt
    "thinking_mode": "adaptive",  # adaptive = erst ohne Denken, bei Fehlschlag mit | auto | always | never
    "thinking_budget": 512,         # Denk-Token-Deckel — wirkt NUR bei Vorlagen, die max_thinking_tokens
                                    # kennen (gemma4/qwen3). Bonsai2 & Co. ignorieren ihn → reasoning_effort
    "reasoning_effort": "low",      # low | medium | xhigh — Länge des Denkens bei Vorlagen mit
                                    # reasoning_effort. Ohne Angabe nehmen diese Vorlagen xhigh (!).
    # In so vielen Runden pro Turn darf gedacht werden. 0 = ALLE Runden (empfohlen!): Vorlagen wie
    # Bonsai2 schreiben den Reasoning-Hinweis GANZ VORNE in den Prompt — jedes Umschalten zwischen
    # „mit/ohne Denken" ändert damit Zeichen 19 und entwertet den gesamten KV-Prefix-Cache inkl.
    # Tool-Block (gemessen: +13 s pro Umschaltung). Ein Limit lohnt nur bei Vorlagen, die den
    # Denk-Schalter erst am Prompt-ENDE abbilden (gemma4/qwen3).
    "thinking_rounds": 0,
    "debug_enabled": False,
    "log_retention_days": 14,       # persistente JSONL-Logs nach N Tagen löschen (#1; 0 = nie)
    "history_retention_days": 90,   # chat_history: Zeilen älter als N Tage löschen (Housekeeping; 0 = nie)
    "history_keep_per_session": 200,  # chat_history: pro Session höchstens N Zeilen behalten
    "log_redact": False,            # Nutzertext/Tool-Args/Ergebnis aus den JSONL-Logs entfernen (Privacy)
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
    # Ereignis-Auslösung von außen (POST /api/admin/events/fire) ohne Admin-Login:
    # nur aktiv, wenn beides gesetzt ist (Toggle + Geheimnis). Verwaltung im Admin-UI → Autonomie.
    "events_token_enabled": False,
    "events_token": "",                         # Geheimnis (Header X-JARVIS-Event-Token / Bearer / ?token=)

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

    # ── Modell-Registry (Phase 0): Rollen-Zuweisung + Slots ──────────────────────
    # Effektiver Kontext pro Anfrage = ctx_total / slots (llama-swap --parallel teilt den Kontext).
    # Beim Speichern im Admin-UI werden llm_model/vision_model/llm_ctx daraus gespiegelt (Rückwärtskompat).
    "models": [
        {"id": "qwen3-14b", "role": "agent", "ctx_total": 40960, "slots": 1},
        {"id": "gemma4-12b", "role": "vision", "ctx_total": 49152, "slots": 2},
    ],
    "subagent_model": "",            # leer = Subagent nutzt das Agenten-Modell (Phase 4)

    # ── Agent-kuratiertes Nutzermodell (Phase 3) ─────────────────────────────────
    "profile_enabled": True,         # rollierendes Nutzerprofil pflegen + in den Prompt einblenden
    "profile_min_interval_s": 180,   # Profil je Nutzer höchstens alle N Sekunden neu generieren (restart-robust)

    # ── Plugin-Gateway (/api/v1/*) — CORS für Browser-PWAs auf anderem Origin ─────
    # Liste erlaubter Origins (Schema+Host+Port), z.B. ["http://192.168.66.224:8096"].
    # "*" = jeden Origin erlauben (ok, da das Gateway Bearer-Keys nutzt, keine Cookies).
    # Origins registrierter Plugins (manifest.ui.entry) werden ZUSÄTZLICH automatisch erlaubt.
    "plugin_cors_origins": ["*"],
    # Rate-Limits pro Plugin-API-Key und Minute (0 = aus). heavy = LLM/Vision/Audio (GPU-Schutz).
    "plugin_rate_limit_per_min": 240,
    "plugin_rate_limit_heavy_per_min": 30,

    # ── Kalender ─────────────────────────────────────────────────────────────────
    "calendar_enabled": True,
    "calendar_base_url": "https://192.168.66.224:8088",   # Basis für iCal-Abo-Links (nur LAN)

    # ── Obsidian-Notizen (pro Nutzer eine Vault) ─────────────────────────────────
    "obsidian_enabled": True,
    "obsidian_note_mode": "file",    # "file" = jede Notiz eine eigene Datei (auto-Titel) | "inbox" = Bullets in einer Sammeldatei
    "obsidian_inbox": "Inbox.md",    # Sammeldatei nur im Modus "inbox"
    "obsidian_autolink": True,       # neue Notizen automatisch per [[Wikilinks]] zu Themen/Notizen verbinden
    "obsidian_autolink_max": 4,      # höchstens so viele automatische Links pro Notiz
    # Kategorie/Thema einer Notiz ("Notiz zum Thema X …"):
    "obsidian_category_folder": True,  # Notiz in gleichnamigen Unterordner der Kategorie ablegen
    "obsidian_category_tag": True,     # Kategorie zusätzlich als Tag im YAML-Frontmatter (+ erstellt:)
    "obsidian_date_prefix": True,      # Dateinamen mit Datum präfixen ("2026-07-01 Titel.md") → chronologisch sortierbar
    "obsidian_daily_categories": ["Tagebuch"],  # Kategorien im Daily-Notes-Modus: an <Kat>/YYYY-MM-DD.md anhängen
    # Zuordnung JARVIS-Nutzername (klein) → absoluter Vault-Pfad auf dem Host.
    "obsidian_vaults": {"daniel": "/opt/obsidian/config/Daniel"},
}


def load() -> dict:
    cfg = dict(_DEFAULTS)
    try:
        cfg.update(json.loads(_CONFIG_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except Exception as e:  # pragma: no cover
        print(f"[config] Warnung: config.json nicht lesbar ({e}) — nutze Defaults.")
    # Secrets optional per Env überschreiben (hält sie aus config.json heraus; erweiterungen.md 3.2)
    env_tok = os.environ.get("JARVIS_TELEGRAM_TOKEN")
    if env_tok:
        cfg["telegram_bot_token"] = env_tok
    return cfg


def get() -> dict:
    with _LOCK:
        return load()


# ── Modell-Registry-Helfer (Phase 0) ─────────────────────────────────────────
def model_for(role: str, cfg: dict | None = None) -> str:
    """Modell-ID für eine Rolle (agent/vision/fast/subagent). Fällt sinnvoll zurück."""
    cfg = cfg or load()
    for m in cfg.get("models") or []:
        if m.get("role") == role and m.get("id"):
            return m["id"]
    if role == "vision":
        return cfg.get("vision_model") or cfg.get("llm_model")
    if role == "subagent":
        return cfg.get("subagent_model") or model_for("agent", cfg)
    return cfg.get("llm_model")


def ctx_for(role: str, cfg: dict | None = None) -> int:
    """Effektives Kontextfenster pro Anfrage für eine Rolle = ctx_total / slots."""
    cfg = cfg or load()
    for m in cfg.get("models") or []:
        if m.get("role") == role and m.get("id"):
            return max(512, int(m.get("ctx_total", 32768)) // max(1, int(m.get("slots", 1))))
    return int(cfg.get("llm_ctx", 32768))


def apply_models(models: list[dict]) -> dict:
    """Registry speichern UND llm_model/vision_model/llm_ctx daraus spiegeln (Rückwärtskompat)."""
    patch = {"models": models}
    agent = next((m for m in models if m.get("role") == "agent" and m.get("id")), None)
    vision = next((m for m in models if m.get("role") == "vision" and m.get("id")), None)
    if agent:
        patch["llm_model"] = agent["id"]
        patch["llm_ctx"] = max(512, int(agent.get("ctx_total", 32768)) // max(1, int(agent.get("slots", 1))))
    if vision:
        patch["vision_model"] = vision["id"]
    return update(patch)


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
