# SH-Mark-XL — Überarbeiteter Umbauplan v3.0

> Review & Erweiterung des Plans aus `umbau.txt` (v2.0).
> Fokus: saubere GPU-Auslagerung, Wiederverwendung des bestehenden Codes,
> realistischere Roadmap und Härtung der Sicherheit.

---

## 0. Kernbefund nach Code-Analyse

Der bestehende `vorlage/`-Code ist **schon halb auf eine verteilte Architektur vorbereitet**.
Das ändert den Plan grundlegend — wir bauen weniger neu, als v2.0 annimmt:

| Baustein | Status heute | Konsequenz |
|----------|--------------|------------|
| **LLM** (`core/llm_client.py`) | spricht Ollama **remote** über `llm_url`, plus OpenAI-kompatibel | GPU-Offloading praktisch fertig → nur URL umstellen |
| **TTS** (`core/tts.py`) | `OpenAISpeechTTSEngine` ruft TTS **per HTTP** (`/v1/audio/speech`) ab; German-Kokoro läuft schon als Docker | Offloading-Muster existiert, muss nur Standard werden |
| **STT** (`core/stt.py`) | `WhisperSTT` läuft **nur in-process** (torch/CUDA lokal) | **Einzige echte Lücke** — braucht einen Remote-STT-Microservice |
| **Vision** (`actions/screen_processor.py`) | Screenshot lokal (mss) → Ollama-Vision-Modell | **Geteiltes Tool**: Capture am Client, Inferenz auf GPU |

**Wichtigste Designentscheidung:** Die 14 OS-nahen Tools in `actions/` (pyautogui, pygetwindow,
mss, Steam/Epic, Dateisystem …) sind ~Tausende Zeilen erprobter Python-Code. Diese in Rust/Tauri
neu zu schreiben wäre ein enormer Aufwand. **Stattdessen: bestehende `actions/` als Python-„Client-Agent"
wiederverwenden, Tauri (optional) nur als UI/Transport-Shell.**

---

## 0.5 QUERSCHNITTSANFORDERUNG: Durchgängig Deutsch (Pflicht)

Das System muss **vollständig auf Deutsch funktionieren** — Erkennung, Antworttext, Sprachausgabe und UI.
Zwei Stolperfallen, die das am häufigsten kaputtmachen:

> **(A)** Ohne expliziten Deutsch-Zwang im System-Prompt antwortet das LLM oft englisch.
> **(B)** Standard-Kokoro-82M hat **keine** deutsche Stimme — ohne den deutschen TTS-Container klingt die Ausgabe nicht deutsch.

| Schicht | Was zu tun ist |
|---------|----------------|
| **STT** (`core/stt.py`) | `stt_language: "de"` (zuverlässiger als `auto`) + deutsches Modell `jimmymeister/whisper-large-v3-turbo-german-ct2`; STT-Microservice muss `language` durchreichen |
| **LLM** (`core/prompt.txt`) | Harte Regel im System-Prompt: „Antworte **immer auf Deutsch**, unabhängig von der Eingabesprache." Tool-`description`-Felder in `TOOL_DECLARATIONS` (main.py) eindeutschen, damit Routing mit deutschen Äußerungen matcht. Modell mit starker DE-Kompetenz (Qwen2.5, Llama-3.x, Mistral) |
| **TTS** (`core/tts.py`) | Default `tts_engine: "ttsserver"` → **deutscher Kokoro-Container** (`docker/german-tts`, `dm_martin`); Cloud-Fallback EdgeTTS `de-DE-KatjaNeural`/`de-DE-ConradNeural` (Code fällt bereits darauf zurück). Optional XTTSv2 als hochwertige DE-Offline-Stimme |
| **Web-UI / Client** | Volle i18n, Default-Locale `de-DE`: Labels, Zustände (LISTENING/THINKING), Fehler-/Statusmeldungen |
| **Sandbox-Tools** | Region/Sprache DE: SearXNG `language=de`, Google `hl=de&gl=DE`, DuckDuckGo `region=de-de`; Zusammenfassungen auf Deutsch |
| **TTS-Normalisierung** | Zahlen/Datum/Abkürzungen deutsch normalisieren („ca."→„circa", Uhrzeiten, Einheiten) — beim DE-Kokoro eingebaut, bei eigenem TTS-Server explizit sicherstellen |
| **Memory (RAG)** | Bei Umstieg auf semantischen Recall ein **multilinguales** Embedding-Modell wählen (z.B. `multilingual-e5`), damit deutsche Erinnerungen korrekt abgerufen werden |

**Empfohlene Default-`config/api_keys.json`:**
```json
{
  "stt_engine": "whisper",
  "stt_model":  "jimmymeister/whisper-large-v3-turbo-german-ct2",
  "stt_language": "de",
  "llm_model":  "qwen2.5:7b",
  "tts_engine": "ttsserver",
  "tts_server_url": "http://german-tts:8080",
  "tts_voice":  "dm_martin"
}
```

---

## 1. Tier-Architektur (statt eines monolithischen „Core-Containers")

v2.0 wirft alles in *einen* Docker-Container. Besser: **vier entkoppelte Tiers**, weil GPU-,
CPU- und OS-Lasten völlig unterschiedliche Hardware/Lifecycles haben.

```
                    ┌──────────────────────────────────────┐
                    │  TIER 2 — INFERENCE (GPU-Server)       │
                    │  docker-compose.gpu.yml                │
                    │   • Ollama  (LLM + Vision/llava,qwen-vl)│
                    │   • STT-Service (faster-whisper, OpenAI │
                    │     /v1/audio/transcriptions)           │
                    │   • TTS-Service (Kokoro/XTTS, /v1/...)  │
                    └───────────────▲────────────────────────┘
                                    │  HTTP/gRPC (internes Netz/VPN)
┌──────────────────┐   ┌───────────┴───────────────┐   ┌────────────────────┐
│ TIER 3 — SANDBOX │◄──┤  TIER 1 — ORCHESTRATOR     ├──►│  Externe MCP-Server │
│ docker-compose   │   │  (CPU, leichtgewichtig)    │   │  (Streamable HTTP)  │
│ • Playwright     │   │  • Tool-Routing-Engine     │   └────────────────────┘
│ • SearXNG        │   │  • Client-Registry         │
│ • Code-Sandbox   │   │  • MCP-Client-Hub          │
│   (gVisor/locked)│   │  • Audio-Stream-Server (WS)│
└──────────────────┘   │  • Web-UI-Backend (FastAPI)│
                       └───────▲────────────▲───────┘
                  mTLS/WSS     │            │  WebRTC/WS (Opus)
            ┌─────────────────┘            └──────────────┐
   ┌────────┴─────────┐              ┌────────────────────┴───┐
   │ CLIENT-AGENT(s)  │              │ Browser-UI / PWA        │
   │ Python-Daemon +  │              │ ESP32-S3-BOX Satellit   │
   │ Tauri-Tray       │              │ (Wake-Word + Audio)     │
   │ (actions/*.py)   │              └─────────────────────────┘
   └──────────────────┘
```

### Warum diese Trennung?
- **Tier 1 (Orchestrator)** ist reines CPU/IO und kann **immer** laufen (kleiner VPS, NAS, Mini-PC).
- **Tier 2 (Inference)** ist die einzige GPU-Last → eigene `docker-compose.gpu.yml`, eigener Server,
  **on-demand** start-/stoppbar (Kostenersparnis, s. §6).
- **Tier 3 (Sandbox)** kapselt unsicheren/LLM-generierten Code getrennt vom Orchestrator.
- **Client-Agent** läuft auf dem End-PC, weil OS-Steuerung physischen Zugriff braucht.

---

## 2. Tool-Routing: 3 Ausführungsziele + Pipelines

v2.0 spricht von „intern vs. extern". Zu grob — wir brauchen **drei** Ziele und **geteilte Tools**.
Jedes Tool deklariert in einer Capability-Registry sein `target`:

| target | Wo | Tools (aus heutigem `actions/`) |
|--------|-----|---------------------------------|
| `inference` | Tier 2 (GPU) | LLM-Antwort, STT, TTS, Vision-Analyse |
| `internal` | Tier 3 (Sandbox) | `web_search`, `browser_control`, `flight_finder`, `weather_report`, `youtube_video`, `code_helper`, `dev_agent` |
| `client` | End-PC | `open_app`, `computer_control`, `computer_settings`, `desktop_control`, `file_controller`, `game_updater`, `reminder`, `send_message`, `file_processor` |
| **pipeline** | Client→GPU | `screen_process` (Capture am Client → Vision auf GPU) |

**Routing-Engine** (neuer `core/router.py`): mappt Tool → target → konkreter Endpunkt
(welcher Client? welche GPU-Instanz?). Bei Mehrdeutigkeit Rückfrage an den Nutzer
(„Auf welchem PC?") — wie in v2.0 vorgesehen, aber als formale Policy.

---

## 2.5 Client-Agent: Multi-OS, alle Funktionen, Berechtigungs-UI

**Anforderung (bestätigt):** Der Client läuft auf **Windows, Linux und macOS**, kann **alle bisherigen
Funktionen** ausführen (inkl. Dateizugriff, App-Steuerung, Steam/Epic, Lautstärke/Helligkeit …), und
der Nutzer kann **Berechtigungen manuell anpassen**.

**Befund:** Die `actions/*.py` sind **bereits multi-OS** (explizite `Windows`/`Darwin`/`Linux`-Zweige;
Windows-only-Libs sauber gekapselt). → Die Cross-Platform-Logik existiert schon und wird **wiederverwendet**,
nicht neu geschrieben.

### Aufbau
```
┌─ Client-Agent (Python, cross-platform, ein Paket pro OS) ──────────┐
│  • Transport:   mTLS-WebSocket-Client zum Orchestrator             │
│  • Executor:    ruft actions/*.py lokal aus (alle bisherigen Tools)│
│  • Permission-Gate:  prüft VOR jeder Ausführung die lokale Policy  │
│  • GUI:         Tray + Fenster zur Berechtigungsverwaltung         │
│  • Profil:      meldet Hostname/OS/Capabilities beim Handshake     │
└────────────────────────────────────────────────────────────────────┘
```

### Berechtigungsmodell (zweistufig — Client ist letzte Instanz)
- **Pro Tool an/aus** (z.B. `file_controller`, `computer_control` …) und **pro Scope**
  (erlaubte Pfade, read-only vs. write, erlaubte Apps).
- **Lokale Durchsetzung:** Der Client prüft jede Anfrage gegen die lokal gespeicherte Policy —
  der Server kann nichts erzwingen, was der Client lokal verboten hat (Zero-Trust am Endgerät).
- **Server-Spiegel:** Scopes zusätzlich als signierte Capability-Tokens (JWT) im Orchestrator,
  damit das Routing gar nicht erst unerlaubte Tools an einen Client schickt.
- **Bestätigungs-Stufe:** destruktive Aktionen (Löschen, Systembefehle) optional „immer fragen" → Popup im Client.
- **Audit-Log** lokal je ausgeführter Aktion.

### Verpackung pro OS
- Python-Agent als eigenständiges Bundle: PyInstaller/`briefcase` → `.exe` (Win), `.app`/`.dmg` (mac), AppImage/`.deb` (Linux).
- Autostart/Tray pro OS; Erstkonfiguration (Server-URL, Zertifikat-Pairing) per Setup-Dialog.

### GUI: Tauri-Tray + Python-Sidecar (entschieden)
Der Client besteht aus **zwei Prozessen in einem Bundle**:
- **Tauri-Shell (Rust+Web)** = Tray-Icon, Berechtigungs- und Status-UI, mTLS-Transport zum Orchestrator,
  Pairing/Setup-Dialog, Bestätigungs-Popups.
- **Python-Sidecar** = führt `actions/*.py` lokal aus (alle bisherigen Tools, schon multi-OS). Tauri
  startet ihn als **Sidecar-Binary** (PyInstaller-Bundle) und spricht ihn über lokale IPC
  (stdin/stdout-JSON oder localhost-WS) an.
- **Vorteil:** moderne, native Tray-UI **ohne** die 14 OS-Tools neu zu schreiben — Rust macht UI/Transport,
  Python macht die Arbeit. Berechtigungs-Gate sitzt zwischen beiden (Tauri prüft Policy → ruft Sidecar nur für Erlaubtes).

---

## 3. GPU-Auslagerung im Detail (die Hauptanforderung)

`docker-compose.gpu.yml` (eigener Server, NVIDIA-Container-Toolkit):

```yaml
services:
  ollama:                       # LLM + Vision (llava / qwen2-vl)
    image: ollama/ollama
    deploy: { resources: { reservations: { devices: [{driver: nvidia, count: all, capabilities: [gpu]}] }}}
    volumes: [ollama_models:/root/.ollama]

  stt:                          # NEU: faster-whisper als OpenAI-kompat. Service
    image: fedirz/faster-whisper-server:latest-cuda   # od. "speaches"
    deploy: { resources: { reservations: { devices: [{driver: nvidia, count: all, capabilities: [gpu]}] }}}

  tts:                          # Kokoro/XTTS (German-Kokoro existiert bereits)
    build: ./docker/german-tts
    # GPU optional — Kokoro läuft auch auf CPU brauchbar
```

**Anpassungen am bestehenden Code (minimal-invasiv):**
1. `core/stt.py`: neue Klasse `RemoteWhisperSTT`, die Audio an `/v1/audio/transcriptions`
   schickt — analog zu `OpenAISpeechTTSEngine`. Lokales `WhisperSTT` als CPU-Fallback behalten.
2. `core/tts.py`: `ttsserver`-Engine wird **Standard** (existiert schon).
3. `core/llm_client.py`: `llm_url` zeigt auf GPU-Server statt `localhost` — **keine Code-Änderung**.
4. `actions/screen_processor.py`: Vision-URL = GPU-Ollama statt `localhost`.

**Fallback-Ketten** (neu, wichtig bei Heim-GPU die nicht 24/7 läuft):
- STT: Remote-Whisper → lokales Vosk (leicht, CPU) → Fehler
- TTS: Kokoro-Server → EdgeTTS (Cloud, kein GPU) → still
- LLM: GPU-Ollama → kleineres CPU-Modell / Cloud → Fehler

---

## 4. Audio-Pipeline — Verbesserungen ggü. v2.0

v2.0: „WebSocket + PCM/Opus". Konkretisierung & Upgrades:

- **Browser → WebRTC statt rohem WebSocket-PCM.** WebRTC bringt Echo-Cancellation,
  Jitter-Buffer und Opus „gratis" und ist der Standard für Echtzeit-Voice. WS bleibt
  Fallback und für ESP32.
- **Barge-In / Interrupt:** Server-seitige VAD (es gibt bereits `_VADBuffer` in `main.py`)
  erkennt, wenn der Nutzer während TTS spricht → TTS sofort stoppen. Großer UX-Gewinn.
- **Wake-Word-Stufe** (neu): `openWakeWord` im Browser/ESP32, damit nicht permanent
  gestreamt wird (Privacy + Bandbreite). ESP32-S3-BOX kann Wake-Word on-device.
- **Streaming-Teiltranskripte:** Vosk liefert bereits Partials (`process_chunk`) — für „Live-Untertitel" im UI nutzen, finale Transkription via Whisper.

---

## 5. Daten & State (v2.0 nennt nur „SQLite/In-Memory")

| Zweck | Empfehlung |
|-------|-----------|
| Client-Registry, Rechte, Audit-Log | **PostgreSQL** (oder SQLite im Single-Node) |
| Session-/Live-State, Pub-Sub Core↔Audio-Worker | **Redis** |
| Langzeitgedächtnis | **pgvector** statt heutiger `memory/long_term.json` → semantischer Recall (RAG) statt flacher Key-Value-Liste. Echte Erweiterung. |

---

## 6. Neue Ideen / Erweiterungen (über v2.0 hinaus)

1. **On-Demand-GPU:** GPU-Server per Wake-on-LAN / Cloud-Autoscale nur bei Bedarf hochfahren;
   Orchestrator bleibt always-on und zeigt „Inferenz startet…". Spart Strom/Cloud-Kosten.
2. **Observability:** strukturierte Logs + Prometheus-Metriken (Latenz STT/LLM/TTS pro Turn)
   + Tracing eines Turns über alle Services. In verteilten Systemen Pflicht.
3. **Multi-User / Auth (entschieden — wird umgesetzt):** OIDC-Login fürs Web-UI; Mandantentrennung
   quer durch alle Schichten: Client-Registry (Geräte gehören einem Nutzer), Langzeitgedächtnis
   (pgvector pro Nutzer-Namespace), Konversationen und Berechtigungs-Scopes pro Nutzer. Capability-Tokens
   tragen die Nutzer-Identität → ein Nutzer kann nur seine eigenen Geräte/Tools ansprechen.
4. **Geteilter Konversationskontext über Geräte:** „Mach auf dem Laptop weiter, was ich am Desktop angefangen habe."
5. **Sicherheit Client-Agent (kritisch):** Der OS-Exec-Daemon ist die größte Angriffsfläche.
   - Allowlist erlaubter Aktionen, **Nutzer-Bestätigung** für destruktive Operationen, Audit-Log, Dry-Run-Modus.
   - Feingranulare Scopes pro Client (Laptop: schreiben; Arbeits-PC: read-only) — wie v2.0 Phase 4, aber als signierte Capability-Tokens (JWT).
6. **Sandbox-Härtung (Tier 3):** read-only rootfs, seccomp, dropped capabilities, kein Host-Netz.
   `code_helper`/`dev_agent` führen LLM-generierten Code aus → idealerweise gVisor/Firecracker, **niemals** im Orchestrator.
7. **Secrets:** `config/api_keys.json` → Env/Docker-Secrets, nicht ins Image backen.
8. **Reproduzierbare Builds & Healthchecks:** versionierte Images, `restart`-Policies, `/healthz` je Service.
9. **MCP-Hub-Absicherung:** Origin-Validierung (v2.0 erwähnt es) auch für die Audio-WS gegen DNS-Rebinding.

---

## 7. Überarbeitete Roadmap (de-risked)

Reihenfolge geändert: **Zuerst die GPU-Auslagerung validieren** (Hauptrisiko & Hauptanforderung),
bevor Multi-Client-Komplexität dazukommt.

### Phase 0 (NEU) — Inference-Tier herauslösen (Woche 1)
- `docker-compose.gpu.yml` mit Ollama + STT-Service + TTS-Service.
- `RemoteWhisperSTT` ergänzen; TTS/LLM auf Remote-URLs umstellen.
- **Ziel/Test:** bestehende `main.py`-App läuft unverändert weiter, aber **alle GPU-Lasten
  laufen auf dem GPU-Server** — kein Funktionsverlust. Das beweist das Offloading früh.

### Phase 1 — Orchestrator-Core + Web-UI + Browser-Audio (Woche 2–3)
- FastAPI-Core: Routing-Engine, MCP-Client-Stub, WebRTC-Audio, Web-Dashboard-Prototyp.
- STT/TTS über den Audio-Layer ans Web-UI.

### Phase 2 — Client-Agent + Registry + Routing (Woche 4–5)
- Python-Client-Daemon, der `actions/*.py` **wiederverwendet** (Tauri-Tray optional).
- mTLS-Handshake mit Hardware-Profil; Client-Registry in DB; UI zeigt Online-Geräte + Standardgerät.
- Routing-Engine schaltet `internal`/`client`/`pipeline` scharf.

### Phase 3 — MCP-Hub + ESP32-Satellit (Woche 6–7)
- MCP Streamable HTTP mit Origin-Validierung; dynamische Server-Registrierung im UI.
- ESP32-Firmware: Wake-Word + Opus-Stream per WebSocket.

### Phase 4 — Härtung, Policy, Observability (Woche 8)
- Capability-Tokens/Scopes, Sandbox-Härtung, Audit-Log, Fallback-Ketten, Metriken/Tracing, Stresstest.

---

## 8. Getroffene Entscheidungen

| # | Thema | Entscheidung | Konsequenz im Plan |
|---|-------|--------------|--------------------|
| 1 | **Sprache** | Deutsch ist **Pflicht** (durchgängig) | §0.5 — DE-Whisper, DE-Kokoro, DE-System-Prompt, DE-UI, Fallback EdgeTTS `de-DE-*` |
| 2 | **GPU-Server** | Eigene **Heim-Hardware** (LAN) | §3 — On-Demand-Start (Wake-on-LAN) + Fallback-Ketten, da evtl. nicht 24/7 an |
| 3 | **Client** | Multi-OS (Win/Linux/mac), **alle** Funktionen, manuelle Berechtigungen | §2.5 — Reuse der multi-OS `actions/*.py`, zweistufiges Permission-Modell |
| 4 | **Client-GUI** | **Tauri-Tray + Python-Sidecar** | §2.5 — Rust macht UI/Transport, Python führt Tools aus; kein OS-Tool-Rewrite |
| 5 | **Nutzer** | **Multi-User + Auth** (OIDC) | §6.3 — Mandantentrennung über Registry, Memory (pgvector-Namespace), Scopes |
