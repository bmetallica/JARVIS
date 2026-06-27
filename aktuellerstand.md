# SH-Jarvis — Aktueller Stand (Wiedereinstiegspunkt)

> Stand: 2026-06-19. Dieses Dokument fasst den lauffähigen Stand zusammen, damit wir nahtlos weiterarbeiten können.
> Detail-Roadmap: [`roadmap.md`](./roadmap.md) · überarbeiteter Plan: [`umbau_v3.md`](./umbau_v3.md)
>
> **Update 2026-06-24:** Autostart-Dienst (`orchestrator/jarvis-orchestrator.service`), Robustheits-/
> Gedächtnis-Fixes und 5 größere Verbesserungen (persistenter Verlauf, Token-Budget, Observability,
> MCP Deferred-Loading, Eval-Harness) — Details: [`orchestrator/CHANGES-2026-06-24.md`](./orchestrator/CHANGES-2026-06-24.md).
> Agenten-Modell ist jetzt **qwen3-14b @ ctx 40960** (gemma4-12b = Vision).
>
> **Update 2026-06-25:** **Pluginsystem** implementiert & getestet — versioniertes Gateway `/api/v1/*`
> (LLM/Vision/STT/TTS/RAG/Storage/Notify/Tools/Scheduler/Event-WS) mit Plugin-API-Keys + Scopes,
> Plugin-Registry/Admin-UI-Endpunkten, KV-Store und Event-Bus. Module: `api_v1.py`, `plugins_registry.py`,
> `plugin_bus.py`. Plan/Doku: [`pluginsystem.md`](./pluginsystem.md), Template: `deploy/plugin-example/`.
> Erstes Ziel-Plugin: ADHS-Family-Helper (extern/PWA).

## Überblick
Aus der Windows-App **Mark-XL** wird **SH-Jarvis**, ein verteiltes, deutschsprachiges KI-Ökosystem.
Lauffähig sind aktuell **Tier 1 (Orchestrator)**, **Tier 2 (GPU-Inferenz)**, **Tier 3-Daten (pgvector)**,
**Pi-Satellit** und **ESP32-S3-Satellit** (beide getestet). Danach: weitere Client-Agent-/GUI-Arbeit, restliche interne Tools.

## Infrastruktur / Netz
- **Orchestrator-Host (Tier 1):** `192.168.66.224`, FastAPI unter **https://192.168.66.224:8088** (selbstsigniertes TLS; HTTPS nötig fürs Browser-Mikrofon).
- **GPU-Server (Tier 2):** `192.168.66.225` (llama.cpp/llama-swap):
  - LLM/Vision **:8080** (OpenAI-kompatibel) — aktiv: `gemma4-12b` (Chat/Tools+Vision), außerdem qwen2.5-7b, qwen3.6-27b, devstral, **nomic-embed-text**.
  - STT **:8001** (faster-whisper, `jimmymeister/whisper-large-v3-turbo-german-ct2`)
  - TTS **:8002** (deutscher Kokoro, Stimme `martin`)
- **Daten:** Postgres+pgvector-Container `shmarkxl-pgvector`, Host-Port **5440** (`deploy/data/docker-compose.yml`).
- **Embeddings laufen lokal** auf dem Orchestrator (fastembed `nomic-embed-text-v1.5`, CPU) — nicht mehr über llama.cpp.

## Tier 1 — Orchestrator (`/opt/JARVIS/orchestrator/`)
FastAPI. Start: `./run.sh` (oder uvicorn auf 0.0.0.0:8088 mit certs/). Startup lädt Embedding-Modell vor.
- `app.py` — Endpoints + Chat-Logik (Tool-Loop, adaptiv, Streaming, WebSocket, Admin, Debug)
- `config.py`/`config.json` — zentrale Config (im Admin-UI editierbar)
- `services.py` — LLM (`llm_call`/`llm_stream`, Denksteuerung), STT/TTS-Proxy, lokale Embeddings
- `tools.py` — interne Tools + Autorisierung + MCP-Routing (`execute_tool` = Debug-Wrapper)
- `timers.py`, `session_hub.py` (Sessions/WS/Verlauf/Identität/Voice-Puffer), `knowledge.py` (Memory+RAG),
  `store.py` (pgvector), `auth.py` (Nutzer/Gruppen/Rechte), `biometrics.py` (Sprecher), `mcp_hub.py`, `debug.py`
- `static/` — Web-UI (`index.html`/`app.js`/`hud.js`/`style.css`) + Admin-UI (`admin.html`/`admin.js`/`admin.css`)

### Endpoints (Auswahl)
`/` UI · `/admin` Admin-UI · `/health` · `/api/chat` · `/api/chat/stream` (SSE) · `/api/stt` · `/api/tts`
· `/ws` + `/ws/satellite` + `/ws/client` (WebSocket: I/O-Routing, ESP-Audio, Client-Aktionen) · `/api/knowledge/*` · `/api/vision`
· `/api/admin/*` (config, users, groups, mcp, debug, voice-enroll, **devices**, **automations**, **autonomy**, **events/fire**)

## Funktionsumfang (lauffähig & getestet)
- **Voice-Loop im Browser:** Mikro → STT → LLM → TTS, mit HUD-Animation (IDLE/LISTENING/THINKING/SPEAKING).
- **Deutsch durchgängig** (STT/LLM/TTS/UI).
- **TTS-Engines** umschaltbar: EdgeTTS (Default, ~0.6 s), Piper-DE (CPU offline), Kokoro (GPU).
- **Tool-Calling** + **adaptiver Denkmodus** (1. Versuch ohne gemma-Reasoning = schnell; bei Fehlschlag Retry mit Denken). Modi `adaptive`(Default)/`auto`/`never`/`always` + Denk-Budget — im Admin-UI.
- **Interne Tools:** Timer (mehrere parallel, Alarm an die erstellende Quelle), get_datetime, weather (Open-Meteo), web_search (ddgs), save_memory, knowledge_search, create_user, link_voice_to_existing_user, **create/list/cancel_automation**.
- **Autonomie (`automations.py`):** JARVIS plant & handelt selbstständig — Trigger zeitgesteuert (once/interval/daily/weekly) **und** ereignisgesteuert (`speaker_recognized`, erweiterbar via `/api/admin/events/fire`). Autonomer Lauf = Agenten-Tool-Loop unter Besitzer-Rechten **+ Admin-Blacklist** (Tools/MCP); „SILENT" unterdrückt Meldung; Ergebnis via `announce` an Zielquelle. Persistent `automations.json`, Scheduler-Loop. Admin-UI-Tab 🤖.
- **Rückkanal & Geräte:** universelle `announce()` (Browser/Pi JSON+lokale TTS, ESP gesprochenes PCM-Streaming; Render-Capability gegen Doppel-TTS). Heartbeat+Telemetrie (ESP **und** Pi) → Admin-UI-Tab 📡 Geräte (online/offline, Raum/Lautstärke/Mic-Gain/RSSI/FW). **Remote-Steuerung pro Gerät** (`POST /api/admin/devices/control`): Lautstärke (%) und Mic-Gain (dB) direkt aus der Admin-UI setzen.
- **ESP32-S3-Sprachsatellit (getestet, `deploy/satellite-esp/`):** Waveshare-Audio-Board, Wake-Word „Jarvis" on-device (esp-sr WakeNet), 16-kHz-Mono-PCM ↔ `/ws/satellite`. esp-sr **AFE** (NS/AGC/AFE-VAD), **Dual-Mic per `#define` umschaltbar**. Task-Architektur: feed/voice/uplink/playback getrennt, AFE-Pause während Wiedergabe, `WIFI_PS_NONE`, getakteter TTS-Stream. **CPU muss auf 240 MHz** (sonst AFE-Feed-Overflow). Lautstärke per Sprache *und* Admin-UI (Software-Gain, harte 90-%-Obergrenze); Mic-Gain in NVS. SoftAP-Captive-Portal fürs WLAN-Setup, 7× RGB-Status, Heartbeat.
- **Ereignisquellen (10):** speaker_recognized, device_connected/disconnected, satellite_listening, timer_elapsed, user_created, voice_enrolled, document_uploaded, memory_saved, mcp_event (extern via `POST /api/admin/events/fire`). Im create_automation-Tool + Admin-UI wählbar.
- **Client-Agent (Phase 2, Protokoll-first):** WS `/ws/client` + Capability-Registry im Hub + Request/Response-Routing (`hub.call_client`). Tools `client_action` (app.launch/shell.run/window/media/fs/clipboard/system) + `list_client_capabilities`; Capability-Gating + Rechte + Autonomie-Blacklist. Referenz-Thin-Client `deploy/client/jarvis-client.py` (Linux-Best-Effort, Win/mac-Zweige). Verifiziert: Agent → Client system.info + shell.run. GUI/Tauri folgt.
- **Recherche-Agent (`research`-Tool):** Web-Suche → mehrere Quellen via `fetch_url` → Synthese mit Quellenangaben [n] + Quellenliste.
- **Vision (`analyze_image`-Tool + `/api/vision`-Upload + Browser-📷):** multimodale Bildanalyse über `vision_model`. GPU-Server hat kein Internet → Orchestrator lädt Bilder selbst und sendet base64-data-URI (externe URLs serverseitig sonst „Failed to download image").
- **Messaging-Kanal Telegram (`messaging.py`):** fester, geräteunabhängiger Weg. Ausgehend (Automatisierungen/Timer/Agent-Tool `send_message`) + eingehend (Long-Poll → Agenten-Loop → Antwort). Pro-Nutzer `users.telegram_chat_id` (Admin-UI Nutzer-Tab) + Standard-Chat-Fallback. Admin: System → Messaging (Token/Enable/Test). Setup: Bot via @BotFather, `/start` liefert Chat-ID.
- **Code-Sandbox (`deploy/sandbox/`, eigener Container — Tier 3):** Tools `run_python`/`run_shell`/`list_/read_workspace_file`. Isoliert (Nicht-root, cap_drop ALL, cgroup-/rlimit-Limits, Timeout); Internet pro Job per Admin-Toggle (`sandbox_allow_network`) via `unshare -rn`; persistentes Workspace je Namespace. Rechte + Autonomie-Blacklist. Start: `cd deploy/sandbox && docker compose up -d --build` (lauscht 127.0.0.1:8090).
- **Gedächtnis + RAG** auf pgvector (per-Nutzer-Namespace `u<id>`, Auto-Recall pro Turn; Dokument-Upload 📚).
- **Sprecher-Biometrie:** Enrollment (Admin-UI + Selbstbedienung), Laufzeit-Erkennung pro Äußerung → Namespace + Anrede + Rechte. Identität pro Sprechblase angezeigt.
- **Konversationelles Onboarding:** unbekannte Stimme → Jarvis fragt „registriert?" → Stimme ergänzen ODER neues Profil anlegen (kein Admin nötig).
- **Auth/Admin-UI** (`/admin`, Login admin/admin → Pflicht-Wechsel): Nutzer/Gruppen/Rechte (Tool-/MCP-Ressourcen), passwortlose Nutzer (Passwort beim 1. Selbst-Login). Menü: System · Nutzer · Gruppen · MCP · Debug.
- **MCP-Hub:** externe MCP-Server (Streamable HTTP) im Admin-UI verwalten; Tools als `mcp__server__tool`; Autorisierung `mcp:<server>` pro Gruppe. Konfiguriert: `smarthome` = Domoticz `http://192.168.66.30:8000/mcp` (27 Tools).
- **Debug-/Trace** (Admin-UI 🐞 ein/aus): zeichnet STT/Turn/LLM/Tool/MCP/Retry/Fehler auf.

## Performance (warm)
Einfache Turns ~1–2 s · Tool-Turns ~2–4 s · MCP mehrstufig ~4–15 s. Kaltstart hängt an llama-swap (Modell-Pinning ist Nutzer-Aufgabe).
**Prompt-Caching** (`cache_prompt`, Default an, Admin-UI): llama.cpp wiederverwendet den KV-Cache des stabilen System-Prompt-Präfix → schnellerer Prefill. Cache bricht ab, wenn sich das Präfix ändert (Tool-Liste/System-Prompt) oder llama-swap das Modell wechselt.

## Aktuelle Nutzer/Daten (DB)
Nutzer: `admin` (PW gesetzt), `daniel`, `test`, `Jordan` (passwortlos). Gedächtnis-Namespaces: `u2` (Daniel: Name/Adresse/Familie), `u3`. Stimmprofile: vom Nutzer live zu pflegen.

## Bekannte offene Punkte / Hinweise
- **Modell-Pinning** in llama-swap (Nutzer) eliminiert Kaltstarts.
- Browser-Mikro nur über HTTPS/localhost (selbstsigniertes Cert akzeptieren).
- Server-Start im Sandbox-Umfeld: `setsid`-detachen; Vordergrund-`sleep`/`pkill` lösen Exit 144 aus.
- Nächste interne Tools offen: Recherche-Agent, browser_control, Vision-Tool (`vision_model=gemma4-12b` schon vorgesehen).

## Tier-Struktur im Repo
`orchestrator/` (Tier 1) · `deploy/gpu/` (Tier 2 Compose) · `deploy/data/` (pgvector) · `deploy/sandbox/` (Tier 3 Code-Sandbox) · `deploy/satellite/` (Pi) · `deploy/satellite-esp/` (ESP32-Firmware) · `vorlage/` (Alt-App, Code-Steinbruch).
