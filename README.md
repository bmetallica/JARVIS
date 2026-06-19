# SH-Jarvis

**Ein verteiltes, durchgängig deutschsprachiges KI-Assistenz-Ökosystem** — selbst gehostet, mit
lokalen Modellen (LLM/STT/TTS), Sprecher-Identität, Gedächtnis/RAG, Autonomie-Automatisierungen
und Sprachsatelliten (Raspberry Pi & ESP32-S3, Wake-Word „**Jarvis**").

> Aus der Windows-App *Mark-XL* wird *SH-Jarvis*: ein offenes, mehrschichtiges System, das auf
> eigener Hardware läuft — kein Cloud-Zwang, keine Daten außer Haus. Alles auf Deutsch.

---

## ✨ Highlights

- 🗣️ **Voice-Loop**: Mikrofon → STT → LLM (mit Tool-Calling) → TTS — im Browser, auf dem Pi und auf dem ESP32.
- 🇩🇪 **Deutsch durchgängig** (STT, LLM, TTS, UI) — bewusste Designvorgabe.
- 🧠 **Gedächtnis & RAG** auf pgvector, pro Nutzer getrennt, mit Auto-Recall und Dokument-Upload.
- 👤 **Sprecher-Biometrie**: erkennt *wer* spricht → eigener Namespace, Anrede und Rechte; konversationelles Onboarding für neue Stimmen.
- 🤖 **Autonomie**: JARVIS plant & handelt selbstständig — zeit- *und* ereignisgesteuert, unter Besitzer-Rechten + Admin-Blacklist.
- 🔌 **MCP-Hub**: externe [Model-Context-Protocol](https://modelcontextprotocol.io)-Server als Tools (z. B. Smart-Home).
- 🖥️ **Client-Agent**: steuert PCs (App-Start, Shell, Fenster, Medien, Zwischenablage …) über eine Capability-Registry.
- 📡 **Sprachsatelliten**: Raspberry Pi & **ESP32-S3** (Waveshare Audio-Board) mit On-Device-Wake-Word, RGB-Status und Rückkanal (Timer/Benachrichtigungen werden am Gerät gesprochen).
- 💬 **Telegram-Kanal**: geräteunabhängig, ein- und ausgehend.
- 🛡️ **Auth & Admin-UI**: Nutzer/Gruppen/Rechte, Geräteverwaltung, Debug-Trace — alles im Browser.

---

## 🏛️ Architektur

SH-Jarvis ist in **Schichten (Tiers)** aufgebaut:

```
                         ┌─────────────────────────────────────────────┐
   Browser-UI  ─────────▶│                                             │
   Pi-Satellit ─────────▶│   Tier 1 · Orchestrator  (FastAPI)          │
   ESP32-Satellit ──────▶│   Chat-Loop · Tools · Auth · Sessions/WS    │
   Telegram    ─────────▶│   Gedächtnis/RAG · Autonomie · MCP-Hub      │
   Client-PCs  ─────────▶│                                             │
                         └───────┬───────────────┬─────────────┬───────┘
                                 │               │             │
                      ┌──────────▼─────┐ ┌───────▼──────┐ ┌────▼─────────┐
                      │ Tier 2 · GPU   │ │ Tier 3 · Data│ │ Tier 3 ·     │
                      │ LLM/Vision     │ │ Postgres +   │ │ Code-Sandbox │
                      │ STT · TTS      │ │ pgvector     │ │ (Container)  │
                      └────────────────┘ └──────────────┘ └──────────────┘
```

- **Tier 1 — Orchestrator** (`orchestrator/`): FastAPI. Chat-Logik (adaptiver Tool-Loop, Streaming/SSE),
  WebSocket-I/O-Routing, Auth/Admin, Gedächtnis, Autonomie, MCP-Routing, Vision, Messaging.
- **Tier 2 — GPU-Inferenz**: OpenAI-kompatibler LLM-/Vision-Server (z. B. llama.cpp/llama-swap),
  STT (faster-whisper, deutsches Whisper-Modell) und TTS (deutsches Kokoro/Piper/EdgeTTS).
- **Tier 3 — Daten & Sandbox**: Postgres + **pgvector** für Gedächtnis/RAG; isolierte **Code-Sandbox** (eigener Container).
- **Edge — Satelliten & Clients**: Pi-/ESP32-Sprachsatelliten und Thin-Client-Agenten auf PCs.

---

## 📂 Repo-Struktur

| Pfad | Inhalt |
|---|---|
| `orchestrator/` | **Tier 1** — FastAPI-App, Web-UI + Admin-UI (`static/`), alle Kern-Module |
| `deploy/gpu/` | Compose/Configs für die GPU-Inferenz (Tier 2) |
| `deploy/data/` | Postgres + pgvector (Tier 3) |
| `deploy/sandbox/` | Code-Sandbox-Container (Tier 3) |
| `deploy/satellite/` | Raspberry-Pi-Sprachsatellit (Python-Thin-Client + systemd) |
| `deploy/satellite-esp/` | **ESP32-S3-Firmware** (ESP-IDF) für das Waveshare-Audio-Board |
| `roadmap.md` · `aktuellerstand.md` | Roadmap (Phasen) und aktueller Gesamtstand (Wiedereinstieg) |

---

## 🧩 Orchestrator — Module (Auswahl)

- `app.py` — Endpoints + Chat-Logik (Tool-Loop, Streaming, WebSocket, Admin, Debug)
- `services.py` — LLM (`llm_call`/`llm_stream`), STT/TTS-Proxy, lokale Embeddings
- `tools.py` — interne Tools + Autorisierung + MCP-Routing
- `session_hub.py` — Sessions/WebSockets/Verlauf/Identität/Geräte-Telemetrie
- `knowledge.py` / `store.py` — Gedächtnis + RAG auf pgvector
- `auth.py` / `biometrics.py` — Nutzer/Gruppen/Rechte · Sprecher-Erkennung
- `automations.py` — Autonomie (zeit-/ereignisgesteuerte Selbstläufe)
- `mcp_hub.py` / `messaging.py` — MCP-Server-Anbindung · Telegram-Kanal

### Endpoints (Auswahl)
`/` Web-UI · `/admin` Admin-UI · `/health` · `/api/chat` · `/api/chat/stream` (SSE) ·
`/api/stt` · `/api/tts` · `/api/vision` · `/api/knowledge/*` ·
`/ws` · `/ws/satellite` (ESP-Audio) · `/ws/client` (Client-Agenten) ·
`/api/admin/*` (config, users, groups, mcp, devices, automations, debug …)

---

## 📡 ESP32-S3-Sprachsatellit

Firmware für das **Waveshare ESP32-S3-AUDIO-Board** (ES7210 Mic-ADC · ES8311 Speaker-DAC · 7× WS2812 RGB).
Wake-Word **„Jarvis"** läuft on-device (esp-sr WakeNet); danach wird sauberes 16-kHz-Mono-PCM an den
Orchestrator (`/ws/satellite`) gestreamt, die Antwort kommt als PCM zurück und wird abgespielt.

- **Audio-Frontend** über die esp-sr **AFE**-Pipeline: Noise Suppression + AGC + VAD, optional **Dual-Mic** (Array) per `#define` umschaltbar.
- **WLAN-Einrichtung via SoftAP/Captive-Portal** (wie Tasmota): SSID/PW, Orchestrator-URL, Raumname → NVS.
- **Rückkanal**: Timer-Alarme/Benachrichtigungen werden am Gerät gesprochen.
- **Lautstärke & Mic-Gain remote** über die Admin-UI *oder* per Sprache („Jarvis, Lautstärke 1–10"); Lautstärke mit **hartem Cap (Verstärkerschutz)**.
- **Heartbeat/Telemetrie** → Admin-Geräteliste (online/offline, Raum, Lautstärke, RSSI, Firmware).

Details, Pins und Build-Anleitung: [`deploy/satellite-esp/README.md`](./deploy/satellite-esp/README.md).

---

## 🚀 Schnellstart

> Voraussetzungen: ein Host für den Orchestrator (Python 3.11+), ein GPU-Server mit OpenAI-kompatiblem
> LLM-/STT-/TTS-Endpoint, sowie Docker für Postgres/pgvector und die Sandbox.

### 1. Daten (pgvector)
```bash
cd deploy/data && docker compose up -d
```

### 2. Orchestrator (Tier 1)
```bash
cd orchestrator
pip install -r requirements.txt
# Konfiguration in config.json bzw. später im Admin-UI (LLM/STT/TTS-URLs, Modelle …)
./run.sh        # uvicorn auf 0.0.0.0:8088 mit selbstsigniertem TLS aus certs/
```
- Web-UI: `https://<orchestrator-host>:8088/`  (selbstsigniertes Cert akzeptieren — Browser-Mikrofon braucht HTTPS)
- Admin-UI: `https://<orchestrator-host>:8088/admin`  — Erst-Login **`admin` / `admin`** → **Passwortwechsel erzwungen**.

### 3. Code-Sandbox (optional, Tier 3)
```bash
cd deploy/sandbox && docker compose up -d --build   # lauscht auf 127.0.0.1:8090
```

### 4. Satelliten
- **Raspberry Pi**: siehe `deploy/satellite/` (Installer + systemd-Service).
- **ESP32-S3**: siehe ESP-README; Build/Flash via VS Code (ESP-IDF-Extension) oder `idf.py build flash monitor`.

---

## 🔐 Sicherheit & Hinweise

- **Selbst gehostet, LAN-orientiert.** TLS ist selbstsigniert; für Produktion ein echtes Zertifikat einsetzen.
- **Admin-Standardlogin** `admin/admin` ist nur ein Seed und erzwingt sofortigen Passwortwechsel — danach keine hartkodierten Nutzer.
- **Rechte-Modell**: Tools/MCP-Server werden pro Gruppe freigeschaltet; Autonomie-Läufe respektieren Besitzer-Rechte **und** eine Admin-Blacklist.
- **Code-Sandbox** ist isoliert (Nicht-root, `cap_drop ALL`, cgroup-/rlimit-Limits, Timeout); Internet pro Job per Admin-Toggle.
- Konfiguriere host-spezifische Adressen/Tokens lokal — committe **keine** Secrets.

---

## 🗺️ Status & Roadmap

Lauffähig: Orchestrator (Tier 1), GPU-Inferenz (Tier 2), pgvector (Tier 3), Browser-Voice-Loop,
Gedächtnis/RAG, Sprecher-Identität, Autonomie, MCP-Hub, Telegram, Code-Sandbox, Vision,
Client-Agent (Protokoll + Thin-Client) sowie **Pi- und ESP32-Sprachsatelliten**.

Aktueller Gesamtstand: [`aktuellerstand.md`](./aktuellerstand.md) · Detail-Phasen: [`roadmap.md`](./roadmap.md).

---

## 📜 Lizenz

[MIT](./LICENSE) © bmetallica

---

<sub>SH-Jarvis · selbst gehostet · Deutsch · made for the Smart Home im Keller 🏠</sub>
