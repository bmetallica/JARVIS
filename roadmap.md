# SH-Mark-XL — Projekt-Roadmap (lebendes Dokument)

> **Stand:** 2026-06-19 · **Version:** 3.0 · **Detailplan:** [`umbau_v3.md`](./umbau_v3.md)
> Dieses Dokument wird im gesamten Projektverlauf laufend aktualisiert. Statuslegende unten.
>
> **Erweiterungs-Roadmap (2026-06-24):** Adaption aus Hermes Agent — Modell-Registry/Slot-Wahl,
> Cross-Session-Recall, selbst-verbessernde Skills, Nutzermodell, Subagenten, eigenes Tool-Calling-Modell:
> [`roadmap-agent-evolution.md`](./roadmap-agent-evolution.md).

## Statuslegende
- `[ ]` offen · `[~]` in Arbeit · `[x]` erledigt · `[!]` blockiert/Entscheidung nötig
- Fortschritt je Phase wird im Phasenkopf als `(erledigt/gesamt)` geführt.

---

## 1. Vision
Transformation von **Mark-XL** (lokale Windows-App) in **SH-Mark-XL**: ein verteiltes, multi-client-
und mehrsprachiges KI-Ökosystem. Zentrales „Gehirn" in Docker, GPU-Lasten ausgelagert, Steuerung
mehrerer Endgeräte über verschlüsselte Clients, Web-UI mit Voice, MCP-Hub und Hardware-Satelliten.

## 2. Getroffene Entscheidungen (Baseline)
| # | Thema | Entscheidung |
|---|-------|--------------|
| 1 | Sprache | **Deutsch = Pflicht**, durchgängig (STT/TTS/LLM/UI) |
| 2 | GPU-Server | **Eigene Heim-Hardware** (LAN) → On-Demand + Fallbacks |
| 3 | Client | **Multi-OS** (Win/Linux/mac), alle bisherigen Funktionen, manuelle Berechtigungen |
| 4 | Client-GUI | **Tauri-Tray + Python-Sidecar** (Reuse von `actions/*.py`) |
| 5 | Nutzer | **Multi-User + Auth** (OIDC, Mandantentrennung) |

## 3. Ziel-Architektur (Kurzform)
4 Tiers: **Inference (GPU)** · **Orchestrator (CPU)** · **Sandbox** · **Client-Agent**. Details §1 in `umbau_v3.md`.

---

## 4. Querschnittsanforderungen (gelten in JEDER Phase)
- `[ ]` **Deutsch durchgängig** — DE-Whisper, DE-Kokoro/EdgeTTS-`de-DE`, DE-System-Prompt, DE-UI, DE-Suche
- `[ ]` **Sicherheit** — mTLS, Capability-Tokens (JWT), lokale Permission-Durchsetzung, Audit-Log, Secrets aus Env
- `[ ]` **Fallback-Ketten** — STT: Whisper→Vosk · TTS: Kokoro→EdgeTTS · LLM: GPU→CPU/Cloud
- `[ ]` **Observability** — strukturierte Logs, Healthchecks je Service, Latenz-Metriken (ab Phase 1)
- `[ ]` **Reproduzierbarkeit** — versionierte Images, `docker-compose`-basiert, `restart`-Policies

---

## Phase 0 — Inference-Tier herauslösen & GPU-Offloading beweisen (6/6) ✅
**Ziel:** Bestehende `main.py`-App läuft unverändert, aber **alle GPU-Lasten** liegen auf dem Heim-GPU-Server — kein Funktionsverlust.
**Zeitfenster:** Woche 1

- `[x]` `docker-compose.gpu.yml`: Ollama (LLM + Vision) mit NVIDIA-Reservation → `deploy/gpu/`
- `[x]` STT-Microservice (`faster-whisper-server`, OpenAI-`/v1/audio/transcriptions`), DE-Modell als Default → in Compose
- `[x]` TTS-Service: deutschen Kokoro-Container in Compose aufgenommen (`tts`, Port 8080)
- `[x]` `core/stt.py`: `RemoteWhisperSTT` + Verdrahtung in `main.py` (`_do_stt`, `_do_reconfigure`), Engine `whisper_server`
- `[x]` Default-Config auf Remote-Endpoints + DE umgestellt (`config/api_keys.json`: STT/LLM/TTS auf GPU-Host)
- `[~]` System-Prompt (`core/prompt.txt`): Deutsch-Zwang **erledigt**. Tool-`description` eindeutschen **bewusst zurückgestellt** (englische Descriptions = zuverlässigeres Tool-Routing; Parameter werden ohnehin englisch extrahiert)

**Verifiziert (2026-06-16):** End-to-End TTS→STT→LLM gegen 192.168.66.225 erfolgreich getestet (DE-Synthese → korrekte DE-Transkription → DE-LLM-Antwort). Echte IDs eingetragen: LLM `Qwen2.5-Omni`, STT `…turbo-german-ct2`, TTS-Stimme `martin`.

**Noch offen (nicht-blockierend, in spätere Phasen verschoben):**
- Vosk-Fallback bei STT-Server-Ausfall → Cross-Cutting „Fallback-Ketten" (Phase 4)
- Vision (`screen_process`) auf llama.cpp-Format umstellen (Modell `gemma4-26b` vorhanden) → Phase 2

**Akzeptanz:** Sprachbefehl auf Deutsch → STT/LLM/TTS laufen auf dem GPU-Server → deutsche Sprachausgabe. GPU-Server aus → Fallbacks greifen ohne Crash.

---

## Phase 1 — Orchestrator-Core + Web-UI + Browser-Audio (3/6) — testbar ✅
**Ziel:** Zentrales CPU-Gehirn mit Web-UI und Voice im Browser.
**Zeitfenster:** Woche 2–3 · **Code:** `orchestrator/` · **UI:** http://192.168.66.224:8088

- `[x]` FastAPI-Orchestrator-Grundgerüst (`app.py`, `config.py`, `services.py`, `/health`, `/api/config|models|chat|stt|tts`)
- `[ ]` `core/router.py`: Routing-Engine mit Tool-Capability-Registry (`inference`/`internal`/`client`/`pipeline`) → nach Phase 2 verschoben (kommt mit den Tools)
- `[~]` Audio-Layer: Browser-Aufnahme (MediaRecorder) → STT → LLM → TTS-Playback **funktioniert** (HTTPS aktiv → `getUserMedia` verfügbar); echtes WebRTC-Streaming + Opus offen
- `[ ]` Server-VAD + Barge-In (Reuse `_VADBuffer`), optional Wake-Word
- `[x]` Web-UI-Prototyp (deutsch): Chat + Mikrofon + TTS-Toggle + ⚙-Einstellungen (Endpoints UI-änderbar) + Health-Statusampeln
- `[ ]` MCP-Client-Stub im Core (Platzhalter für Phase 3)

**Meilenstein erreicht:** Chat + Voice durch den Orchestrator gegen gemma4/STT/TTS **end-to-end verifiziert** (per curl). UI im Browser testbar.
**Akzeptanz (Rest):** Browser-Voice-Test durch Nutzer; Turn-Latenz messen; WebRTC + VAD/Barge-In nachrüsten.

---

## Phase 1.5 — Interne Fähigkeiten + Tool-Fundament (internal-first) (3/?) — läuft
**Ziel:** Agent kann serverseitig handeln (ohne Client). Tool-Calling-Loop, quellen-bezogenes
I/O-Routing und die internen Tools der Vorlage in den Container holen.
**Entscheidung:** interne Fähigkeiten VOR dem Client ausbauen (Nutzerwunsch).

**Fundament (erledigt):**
- `[x]` **Tool-Calling-Loop** im Orchestrator (`services.llm_call` + Loop in `app.py`, OpenAI-Functions). Modell-Check: gemma4-12b ✅ und qwen2.5-7b ✅ können Tools (Qwen2.5-Omni ❌)
- `[x]` **Session-Hub + WebSocket** (`session_hub.py`, `/ws`): quellen-bezogenes I/O-Routing — asynchrone Ausgaben gehen an die **Ursprungsquelle** (Browser, später ESP32). Reconnect-Puffer.
- `[x]` **Timer-Tool** (`timers.py`, `tools.py`): mehrere **parallele** Timer, je Session, Alarm wird an die erstellende Quelle gepusht + dort gesprochen. End-to-End verifiziert.
- `[x]` **Universeller Rückkanal** `announce()` (kanal-bewusst): Browser/Pi → JSON-Event (lokale TTS), ESP → gesprochenes PCM-Streaming. Render-Capability pro Quelle (`pcm`/`local`) verhindert Doppel-TTS. Genutzt von Timer/Wecker/Automatisierungen/Events.
- `[x]` **Autonomie / Automatisierungen** (`automations.py`): JARVIS plant & handelt selbstständig — zeitgesteuert (once/interval/daily/weekly) **und** ereignisgesteuert (z.B. `speaker_recognized`). Tools `create_/list_/cancel_automation`; persistent (`automations.json`); Scheduler-Loop + Event-Dispatch (Cooldown). Autonome Läufe nutzen den Agenten-Tool-Loop unter Besitzer-Rechten **+ Admin-Blacklist** (Tools/MCP). „SILENT" = keine Meldung. Admin-UI-Tab 🤖. End-to-End verifiziert (Lauf/Blacklist/SILENT/Event).

**Interne Tools:**
- `[x]` `get_datetime` (Europe/Berlin), `weather` (Open-Meteo, keyless, mit Retry), `web_search` (ddgs, DE) — alle end-to-end verifiziert
- `[x]` **`fetch_url`** — konkrete Seite laden + Titel/Überschriften/Lesetext extrahieren (lxml), **SSRF-Schutz** (keine internen/loopback/link-local Ziele). Für News bevorzugt vor web_search. Verifiziert (echte heise.de-Schlagzeilen).
- `[x]` **Messaging-Kanal Telegram** (`messaging.py`): fester, geräteunabhängiger Kommunikationsweg. Ausgehend (Automatisierungen/Timer/Agent `send_message`) **und** eingehend (Polling → Agenten-Loop → Antwort). Pro-Nutzer-Zuordnung `users.telegram_chat_id` (Admin-UI), Standard-Chat als Fallback. Admin-Config + Test. Verifiziert: Zuordnung/Auflösung (Chat-ID↔Nutzer); Live-Senden braucht Bot-Token.
- `[ ]` weitere API-Tools: Übersetzung, Währung, News/RSS
- `[x]` **`browser_control`** (headless Chromium/Playwright in der Sandbox): Tools `browse` (JS-gerendert + Links), `browser_click` (per Text), `browser_type` (+submit, für Suche/Login), `browser_screenshot` (→Vision). Persistente Sitzung je Namespace (Cookies/Logins bleiben). End-to-End verifiziert (example.com, Klick→iana.org, Wikipedia-Suche, Screenshot-Analyse).
- `[x]` **Gedächtnis auf pgvector** (`store.py`/`knowledge.py`): `save_memory`-Tool + **Auto-Recall** pro Turn (semantisch, nomic-embed-text 768d). Verifiziert.
- `[x]` **RAG-Wissensbasis** (gleicher pgvector-Store, `kind='document'`): Upload-Endpoint + UI-Button (📚, txt/md/pdf), `knowledge_search`-Tool. Verifiziert.
- `[x]` **Recherche-Agent** (`research`-Tool): Web-Suche → mehrere Quellen via `fetch_url` → LLM-Synthese mit Quellenangaben [n] + Quellenliste. Verifiziert (Python-3.13-Neuerungen, korrekte Zitate).
- `[x]` **Code-Sandbox / `dev_agent`** (`deploy/sandbox/`, eigener Container): Tools `run_python`/`run_shell`/`list_/read_workspace_file`; isoliert (Nicht-root, cap_drop ALL, cgroup-Limits, Job-Timeout), Internet pro Job ab-/zuschaltbar (Admin) via `unshare -rn`, persistentes Workspace je Namespace. Rechte + Autonomie-Blacklist. **End-to-End verifiziert** (Agent rechnete autonom per Code; Netz-Toggle wirkt).
- `[x]` **Vision-Tool** (multimodal, vision_model): `analyze_image`-Tool (Bild-URL) + `/api/vision`-Upload + Browser-📷-Button. **Wichtig:** GPU-Server hat kein Internet → Orchestrator lädt Bilder und sendet sie als base64-data-URI. Verifiziert (Python-Logo korrekt erkannt, Tool + Chat-Pfad).

**Akzeptanz:** Nutzer kann per Sprache/Text interne Tools auslösen; Ergebnisse/Alarme erscheinen an der richtigen Quelle.

---

## Phase 1.7 — Sprecher-Identität & Autorisierung ✅ (Feinschliff offen)
**Ziel:** Der Agent erkennt **wer spricht** und nutzt das für (a) getrenntes Gedächtnis/Wissen pro Person
und (b) Tool-/MCP-Berechtigungen (z.B. nur Daniel + Johanna dürfen Smart-Home-Tools, das Kind nicht).
**Nutzerwunsch.** Baut auf dem schon vorhandenen `namespace`-Feld (store.py) + Tool-Loop-Hook auf.

**Identität → Namespace → Rechte:** Erkannte Person ⇒ `user_id` ⇒ `namespace` (Memory/Wissen getrennt;
plus gemeinsamer `shared`-Namespace) ⇒ `role` (für Autorisierung). **Wichtig:** Identität wird **pro Äußerung**
bestimmt (am Satelliten wechselt der Sprecher), nicht fix pro Verbindung.

**Inkrementeller Plan:**
- `[x]` **Schritt 1 — Auth & Admin-UI:** Postgres-Tabellen `users/groups/user_groups/group_permissions` (`auth.py`),
  Login mit Session-Cookie (PBKDF2-Hash), Seed **admin/admin** mit erzwungenem Passwortwechsel. **Admin-UI** unter `/admin`
  (verlinkt im Haupt-UI 🛡, passwortgeschützt): Nutzer/Gruppen CRUD + Rechte (Tool-/MCP-Ressourcen) per Checkbox.
  Verifiziert. **Keine hartkodierten Nutzer** — alles über das UI.
- `[x]` **Schritt 2 — Autorisierungs-Durchsetzung:** `auth.is_tool_allowed(user_id, "tool:X")` im Tool-Loop. Modell:
  Tool ist offen, bis eine Gruppe es listet → dann nur Admin/berechtigte Gruppen. Durchsetzung **bei Ausführung**
  (Tool wird angeboten, aber verweigert → **ehrliche Absage statt Halluzination**). Verifiziert.
- `[x]` **Schritt 3 — Stimm-Erkennung (Voice-Biometrie):** `biometrics.py` (resemblyzer, 256-dim, CPU; ffmpeg für Decode).
  Tabelle `voiceprints` (pgvector, ref. user_id). **Enrollment im Admin-UI** (🎙 aufnehmen, mehrere Samples, Zähler).
  Laufzeit: `/api/stt` erzeugt Embedding aus demselben Audio → 1:N-Vergleich → user_id oder „Gast" (Schwelle `voice_id_threshold`=0.75). Verifiziert (gleiche Stimme 0.91, fremde → Gast).
- `[x]` **Schritt 4 — Verknüpfung:** Sprecher-ID wird **pro Äußerung** in der Session gesetzt (server-seitig, nicht client-fälschbar),
  steuert namespace (Memory `u{id}`/`guest`), Anrede und Tool-Rechte. Verifiziert. (Web-Tippen ohne Voice = Gast; Login/OIDC → Phase 4.)

**Offen/Feinschliff:** per-User-Wissensbasis (RAG aktuell shared `default`), höhere Konfidenz+Bestätigung für sensible Aktionen, Anti-Spoofing.

**Sicherheit/Privacy:** Voiceprints sind biometrische Daten → nur lokal speichern. Stimme ist fälschbar (Replay/Imitat)
→ für sensible Aktionen (Smart-Home, Türen) höhere Konfidenz + Bestätigung verlangen.

---

## Phase 2 — Client-Agent + Registry + Routing scharf (4/7) — läuft
**Ziel:** Multi-OS-Client führt alle bisherigen Tools aus, Berechtigungen manuell steuerbar.
**Zeitfenster:** Woche 4–5 · **Entscheidung (Nutzer):** Protokoll + Registry zuerst, GUI später.

- `[x]` **Client-Protokoll + Registry + Routing** (server-seitig): WS-Endpoint `/ws/client`, Capabilities-Registry im Hub, **Request/Response-Korrelation** (`hub.call_client`/`resolve_call`), Ziel-Auflösung (Gerät/Session/einziger Client). Agent-Tools `client_action` (Aktions-Enum) + `list_client_capabilities`; Capability-Gating + Tool-Rechte + Autonomie-Blacklist. End-to-End verifiziert (system.info, shell.run).
- `[x]` **Python-Sidecar (Thin-Client)** `deploy/client/jarvis-client.py`: registriert Capabilities, führt Aktionen aus. **Ausgebaut auf 31 Aktionen:** app.launch/close, shell.run, open.url/path; window.list/focus/close/minimize/maximize, screenshot, input.type/hotkey, notify; media.play_pause/next/prev/stop/volume, volume.mute, system.info/lock/suspend; fs.read/write/append/list/mkdir/delete, clipboard.get/set (Linux-Best-Effort über wmctrl/xdotool/playerctl/pactl/grim·scrot/notify-send; Win/mac-Zweige). `fs.delete` standardmäßig `deny`.
- `[x]` **`pipeline`-Routing (screen_process)** via `client_screenshot`-Tool: Screenshot LOKAL am Client aufnehmen → an die **Vision-GPU** zur Analyse. Verkettet Client-Capture + multimodales Modell.
- `[~]` **Tauri-Shell** (`deploy/desktop/`, Tauri v2): Tray + Sidecar-Supervisor (startet/überwacht `jarvis-client.py` oder PyInstaller-Binary, übergibt Server/Name/Policy/Audit via Env) + Einstellungs-/Setup-Fenster. Vollständiges Projekt + Build-Anleitung je OS (Win/Linux/mac). **Hier nicht kompilierbar (keine Rust/GUI)** → Build/Feinschliff beim Nutzer. (Pairing/mTLS noch offen.)
- `[~]` Berechtigungs-UI: **lokale Policy-Datei (`policy.json`) + Scopes** (allow/ask/deny je Aktion, fs_read/write_roots, deny-Liste; deny → nicht als Capability gemeldet) **+ grafischer Policy-Editor in der Tauri-App**. Feinkörnige Scope-UI später.
- `[x]` **Permission-Gate + Bestätigung + lokales Audit-Log** (client-seitig, letzte Instanz): `gated_act` prüft Policy vor Ausführung; `ask` → Dialog (zenity/kdialog/osascript/MessageBox/Terminal), **ohne Kanal fail-safe deny**; `audit.log` mit Zeitstempel. End-to-End verifiziert (allow=system.info ausgeführt; ask=shell.run ohne Kanal verweigert; deny=shell.run nicht angeboten + server-seitiges Capability-Gating greift).
- `[~]` Bundling: **Anleitung steht** (PyInstaller-Sidecar + `cargo tauri build` → .deb/.AppImage, .msi/.exe, .app/.dmg); ausführen/signieren beim Nutzer je OS.

**Akzeptanz:** Von zwei verschiedenen OS-Clients lässt sich je eine OS-Aktion auslösen; ein im Client deaktiviertes Tool wird lokal verweigert (auch wenn der Server es schickt).

---

## Phase 3 — MCP-Hub + ESP32-Satellit (3/4) — Voice-Interface live ✅
**Ziel:** Wissenserweiterung via MCP und raumbasiertes Voice-Interface.
**Zeitfenster:** Woche 6–7

- `[x]` **MCP-Client (Streamable HTTP)** — `mcp_hub.py`: Server in DB (`mcp_servers`), Tools gecacht und dem Agenten als
  `mcp__<server>__<tool>` angeboten, Aufruf-Routing. Verwaltung im **Admin-UI** (Tab MCP: hinzufügen/entfernen/aktualisieren, Tool-Zähler/Status).
- `[x]` **MCP-Autorisierung** — jeder Server = Ressource `mcp:<server>`; pro Gruppe freigebbar. Verifiziert (Smart-Home nur „Eltern", Gast verweigert). Getestet gegen Domoticz-MCP (27 Tools, echte Gerätesteuerung).
- `[ ]` MCP-Sicherheit: Origin-Validierung (DNS-Rebinding) auch für Audio-WS; stdio-Transport (lokal)
- `[x]` **Satelliten gebaut** — `sat-pi.md` (Raspberry Pi) und `sat-esp.md` (Waveshare ESP32-S3-AUDIO-Board: WakeNet „Jarvis", ES7210/ES8311, 7× RGB). Reihenfolge wie geplant: Pi zuerst, dann ESP32-S3 — **beide laufen**.
- `[x]` **Pi-Satellit** (`deploy/satellite/`): openWakeWord „hey_jarvis", Aufnahme→`/api/stt`→`/api/chat/stream`→`/api/tts`, WS-Rückkanal. **Auf Pi-Hardware getestet** (Jabra SPEAK 410, HW-Volume via amixer; Erkennungsschwelle 0.65 + Stimm-Nachenrollen).
- `[x]` **`set_device_volume`-Tool + Push** (Voice „Lautstärke 1–10") — verifiziert; zusätzlich **Remote-Steuerung pro Gerät über die Admin-UI** (`POST /api/admin/devices/control`: Lautstärke % + Mic-Gain dB → Push `set_volume`/`set_mic_gain`).
- `[x]` **Orchestrator-Endpoint `/ws/satellite`** (`app.py`): Binär-Audio (PCM s16le 16k) rein → STT+Sprecher → Chat → TTS-PCM raus; reuse `session_hub`. **Auf echter ESP-Hardware verifiziert**; TTS-Stream wird getaktet gesendet (sonst WS-Abbruch beim ESP).
- `[x]` **ESP32-Firmware** (`deploy/satellite-esp/`, ESP-IDF) — **gebaut, geflasht, auf dem Waveshare-Board getestet**: Wake-Word „Jarvis", SoftAP-Captive-Portal, 7× RGB, **Lautstärke 50%-Default/90%-Cap** (Software-Gain) + remote Mic-Gain (NVS). Audio über esp-sr **AFE** (NS/AGC/AFE-VAD), **Dual-Mic per `#define` umschaltbar**. Task-Architektur feed/voice/uplink/playback getrennt, AFE-Pause während Wiedergabe, `WIFI_PS_NONE`. **Wichtig: CPU 240 MHz** (sonst AFE-Feed-Overflow). Im Download-Center (Quellcode-Tar ohne Build-Artefakte).
- `[x]` **Satellit-Heartbeat → Admin-Geräteliste** (online/offline, Raum/Lautstärke/Mic-Gain/RSSI/FW) und **gesprochene Timer-Alarme/Benachrichtigungen an ESP/Pi** (Server streamt TTS-PCM bzw. JSON auf Push).

**Akzeptanz:** Ein externer MCP-Server wird im UI registriert und seine Tools sind im Agenten nutzbar; ESP32 löst per Wake-Word einen vollständigen DE-Voice-Turn aus.

---

## Phase 4 — Multi-User, Härtung, Observability, Stresstest (0/6)
**Ziel:** Produktionsreife.
**Zeitfenster:** Woche 8

- `[ ]` OIDC-Login + Mandantentrennung (Registry, Konversationen, Scopes pro Nutzer)
- `[ ]` Capability-Tokens (JWT) tragen Nutzer-Identität; nutzerfremde Geräte/Tools gesperrt
- `[ ]` Sandbox-Härtung (Tier 3): read-only rootfs, seccomp, dropped caps; Code-Exec isoliert (gVisor/Firecracker)
- `[ ]` pgvector-Langzeitgedächtnis mit multilingualem Embedding (DE-Recall) statt `long_term.json`
- `[ ]` Observability: Prometheus-Metriken + Turn-Tracing über alle Services
- `[ ]` Stresstest: paralleler MCP-Abruf + Container-Suche + Datei-Ablage auf spezifischem Client

**Akzeptanz:** Zwei Nutzer arbeiten isoliert; Latenz unter Last gemessen; Audit-Log lückenlos.

---

## 5. Backlog / Ideen (noch nicht eingeplant)
- `[ ]` On-Demand-GPU per Wake-on-LAN automatisieren (Core startet GPU-Server bei Bedarf)
- `[ ]` Geteilter Konversationskontext über Geräte („mach auf dem Laptop weiter")
- `[ ]` Cloud-GPU als Überlauf/Fallback (Hybrid)
- `[ ]` XTTSv2 als zusätzliche hochwertige DE-Offline-Stimme
- `[ ]` Live-Teiltranskripte im UI (Vosk-Partials)
- `[ ]` CI/CD-Pipeline + automatische Image-Releases
- `[x]` **Endpoints im Web-UI einstellbar** — ⚙-Panel im Orchestrator-UI (LLM/STT/TTS-URLs, Modelle, TTS-Engine, Stimmen, System-Prompt; persistiert)
- `[x]` **UI-Animation/HUD** — Canvas-Orb (`static/hud.js`) im Vorlagen-Stil: Halo, Pulsringe, 3 rotierende Bogenringe, Scanner, Tick-Marken, Fadenkreuz, Partikel, Waveform. Zustände IDLE/LISTENING/THINKING/SPEAKING farbgesteuert, an Mikro/LLM/TTS gekoppelt
- `[ ]` **TTS-Streaming** — satzweise sprechen, sobald erster Satz fertig (versteckt LLM-Denkzeit)

---

## 6. Änderungslog
| Datum | Änderung |
|-------|----------|
| 2026-06-16 | Roadmap v3.0 erstellt; Entscheidungen 1–5 fixiert; Phasen 0–4 definiert |
| 2026-06-16 | Phase 0 gestartet: `deploy/gpu/` (Compose + .env.example + README); `RemoteWhisperSTT` + Verdrahtung; Config auf Remote/DE; Prompt-Deutsch-Zwang |
| 2026-06-16 | Reale GPU-Topologie eingearbeitet (192.168.66.225): LLM=llama.cpp:8080 (`openai`), STT:8001, TTS:8002. Compose: TTS→8002, Ollama optional (`--profile ollama`) |
| 2026-06-16 | **Phase 0 ✅** — GPU-Tier End-to-End verifiziert (TTS→STT→LLM). Config-IDs korrigiert: LLM `Qwen2.5-Omni`, TTS-Stimme `martin`, STT DE-Turbo-Modell bestätigt |
| 2026-06-16 | **Phase 1 (testbar)** — Orchestrator `orchestrator/` (FastAPI): `/health`, `/api/chat|stt|tts|config|models` + Web-UI (Chat/Voice/⚙). Modell auf **gemma4-26b** umgestellt (Reasoning-Modell → `content` von `reasoning_content` getrennt). Läuft auf 192.168.66.224:**8088** (8000 belegt). Alle Endpoints per curl verifiziert |
| 2026-06-16 | HTTPS aktiviert (selbstsigniert, `gen_cert.sh`/`run.sh`) → Browser-Mikrofon funktioniert. Server via `setsid` abgekoppelt gestartet (Foreground-`sleep`/`pkill` lösen im Harness Exit 144 aus) |
| 2026-06-16 | STT-500 (CUDA-OOM): gemma4-26b frisst VRAM → großes Whisper-Modell passt nicht daneben. **Gelöst:** LLM auf `gemma4-12b` → großes deutsches Turbo-Whisper passt daneben, transkribiert DE wortgenau. Voller Voice-Pfad (Chat+STT+TTS, deutsch) verifiziert ✅ |
| 2026-06-16 | TTS-Qualität/Tempo: **EdgeTTS** (`de-DE-ConradNeural`, ~0.6–1.3 s, mp3) als Default statt Kokoro (2–7 s) — wählbar im ⚙-UI. Leer-Antwort-Guard (kein TTS-400 mehr). Latenz-Analyse: LLM-Verzögerung großteils **llama-swap-Modellwechsel** (warm ~6 s). Streaming als nächster Latenz-Hebel offen |
| 2026-06-16 | **3 TTS-Engines wählbar** im ⚙-UI: edge (Cloud), **piper-DE (CPU offline, ~0.1 s warm)**, kokoro (GPU). Per-Engine-Stimmen. **Rebranding Mark/Mark-XL → JARVIS** (UI, Prompt, Code). UI-Animation/HUD als Backlog notiert |
| 2026-06-16 | „Leere Antwort" bei längeren Nachrichten: gemma4-Reasoning fraß das Token-Budget (512). Fix: `llm_max_tokens`↑1024 **und** `llm_timeout` im ⚙-UI einstellbar; bei abgeschnittener Antwort jetzt präzise Fehlermeldung statt „leer". Verifiziert: lange Nachricht liefert vollständige DE-Antwort |
| 2026-06-16 | **UI-HUD-Animation** (`static/hud.js`): Canvas-Orb im JARVIS-Stil (Halo/Pulsringe/Bogenringe/Scanner/Partikel/Waveform), Zustände IDLE/LISTENING/THINKING/SPEAKING an Mikro·LLM·TTS gekoppelt. Statische Files live (kein Neustart nötig) |
| 2026-06-16 | **Phase 1.5 gestartet (internal-first):** Tool-Calling-Loop + Session-Hub/WebSocket (`/ws`, quellen-bezogenes I/O-Routing) + Timer-Tool (mehrere parallel, je Session, Alarm an Ursprungsquelle). End-to-End verifiziert. Modell-Check: gemma4-12b & qwen2.5-7b können Tools |
| 2026-06-16 | Interne Tools ergänzt: `get_datetime`, `weather` (Open-Meteo keyless + Retry gegen transiente Aussetzer), `web_search` (ddgs, DE). Alle durch den Orchestrator verifiziert |
| 2026-06-16 | **Gedächtnis + RAG auf pgvector**: Postgres+pgvector-Container (`deploy/data`, :5440), `store.py`/`knowledge.py`, Embeddings via nomic-embed-text (768d). `save_memory`+Auto-Recall, `knowledge_search`+Upload (📚, txt/md/pdf). End-to-End verifiziert. **Neuer Plan: Phase 1.7 Sprecher-Identität & Autorisierung** (per-User-namespace + Tool-Rechte) |
| 2026-06-16 | **Phase 1.7 Schritt 1 — Auth & Admin-UI** (`auth.py`, `/admin`, `admin.html/js/css`): Login (PBKDF2 + Session-Cookie), Seed admin/admin + erzwungener PW-Wechsel, Nutzer/Gruppen/Rechte-CRUD (Tool-Ressourcen). Keine hartkodierten Nutzer. Verifiziert, DB sauber zurückgesetzt (nur admin) |
| 2026-06-16 | **Phase 1.7 Schritte 2–4 ✅**: Autorisierung im Tool-Loop (offen-bis-eingeschränkt, ehrliche Absage); **Sprach-Biometrie** (`biometrics.py`, resemblyzer 256d, ffmpeg, `voiceprints`-Tabelle, Enrollment im Admin-UI); Laufzeit-Sprecher-ID in `/api/stt` (pro Äußerung, server-seitig) → namespace+Anrede+Rechte. End-to-End verifiziert (Stimme→benjamin 0.91, fremd→Gast). DB sauber (admin/admin) |
| 2026-06-16 | **Fix Sprecherwechsel:** Identität/Verlauf hingen an der Browser-Session (mitgesendeter Verlauf). Jetzt führt der **Server den Verlauf pro Session** und setzt ihn bei **Sprecherwechsel** zurück; Identität strikt pro Äußerung aus der Stimme. **Identität pro Sprechblase** im UI (Name+Konfidenz / „nicht erkannt"). Verifiziert: Wechsel Daniel↔test in einer Session schaltet Gedächtnis korrekt um. (Hinweis: Auth-DB beim Debuggen zurückgesetzt — Konten/Stimmprofile/Admin-PW weg, Gedächtnis u2/u3 erhalten) |
| 2026-06-17 | **UI-Umbau + Selbstbedienung + Onboarding:** zentrale Einstellungen in **Admin-UI** (Menü: System/Nutzer/Gruppen/MCP, hinter Admin-Login); normales UI hat Profil-Panel (👤): passwortlosen Nutzer anlegen + Stimme des zuletzt Erkannten ergänzen. **Passwortlose Nutzer** (Passwort beim 1. Selbst-Login). **Konversationelles Onboarding**: bei unbekannter Stimme fragt Jarvis „registriert?" → ergänzt bestehenden Nutzer oder legt neues Profil an + hinterlegt Stimme (kein Admin nötig). Sprachbefehl-Tool `create_user`. Alles verifiziert |
| 2026-06-17 | **MCP-Hub ✅** (`mcp_hub.py`): Streamable-HTTP-Client, Server-Verwaltung im Admin-UI, Tools als `mcp__server__tool`, Autorisierung `mcp:<server>` pro Gruppe. Gegen Domoticz-MCP (27 Tools) verifiziert inkl. Rechteprüfung |
| 2026-06-17 | **Performance-Optimierung (~5–10×):** (1) **Embeddings lokal** (fastembed nomic-v1.5, CPU) statt über llama.cpp → killt den doppelten llama-swap pro Turn; bestehende Vektoren neu eingebettet. (2) Modell-Wahl (s. nächster Eintrag). (3) **Streaming-TTS** (`/api/chat/stream` SSE, `services.llm_stream`) — Satz-für-Satz, TTS-Queue im UI; erster Satz nach ~0,9 s. Offen: MCP-Session-Reuse (gering), Modell-Pinning (Nutzer) |
| 2026-06-17 | Fix: `auto`-Denkmodus war zu eng (nur Onboarding) → MCP-Abfragen scheiterten ohne Denken. `MAX_TOOL_STEPS` 5→8 |
| 2026-06-17 | **Pi-Satellit-Client** (`deploy/satellite/`) gebaut: Wake-Word „Jarvis" (openWakeWord) → STT/Chat-Stream/TTS, WS-Rückkanal. `set_device_volume`-Tool + `set_volume`-Push verifiziert. Syntax/Protokoll geprüft; On-Device-Audio-Test (Pi 3B+) offen |
| 2026-06-17 | Satellit läuft auf Pi. Fixes: openWakeWord-Modell-Download im Installer; `numpy<2` (tflite-Kompat); PortAudio-Geräte-Fallback + Liste; **Signaltöne** (Alexa-artig: Start/Ende); **Lautstärke via ALSA-`amixer`** (statt ffplay-Software, regelt echte HW-Lautstärke; `alsa_card`); **deterministisches Onboarding im Server** (unbekannte Stimme → feste Frage „registriert?" → Antwort mit Denken verarbeitet → link/create). Onboarding end-to-end verifiziert |
| 2026-06-18 | Satellit-Audio final: USB-Speakerphone (Jabra SPEAK 410) für **Ein- UND Ausgang** (asound.conf `pcm.!default plug→hw:CARD=USB`), echte HW-Lautstärke via `amixer -c USB PCM`. TTS-Wiedergabe via ffmpeg→sounddevice. Erkennung verbessert: Schwelle 0.75→**0.65**, neues Tool **`remember_my_voice`** (über Satellit/Jabra Stimmproben ergänzen — löst Mikrofon-Mismatch). **Kanal-Bewusstsein**: Session-`client_type` fließt in den Prompt — Satellit = nur Audio, knapp sprechen, keine Browser/Fenster/URLs. Verifiziert |
| 2026-06-17 | **Debug-/Trace-Funktion** (`debug.py`, im Admin-UI 🐞 ein/aus, persistiert `debug_enabled`): Ring-Puffer zeichnet STT, Turn (Identität/Modus/Denken), jeden LLM-Aufruf (Dauer/Tool-Calls), jeden Tool-/MCP-Aufruf (Name/Args/Ergebnis/ms), Retries, Fehler. Anzeige im Admin (Aktualisieren/Leeren/Auto-Refresh). Verifiziert |
| 2026-06-17 | **Adaptiver Denkmodus** (`thinking_mode=adaptive`, neuer Default): 1. Versuch ohne Denken (schnell); bei Fehlschlag (leer/zu viele Schritte/Fehler) automatischer Retry **mit** Denken. `_run_loop`-Helfer; Streaming: schneller Vorab-Versuch nicht-gestreamt, sonst gestreamter Denk-Lauf. Ergebnis: einfache Turns ~2 s, MCP oft ~4 s (sonst Retry). Modi adaptive/auto/never/always im Admin-UI |
| 2026-06-17 | **Modell + Denksteuerung:** qwen2.5-7b zu schwach bei mehrstufigen Tool-/Identitäts-Flows → zurück auf **gemma4-12b**. gemma4-Reasoning ist der Grund für korrekte Tool-Wahl, kostet aber ~5–8 s/Turn. **Hybrid: gemma4-Denken pro Request steuerbar** (`chat_template_kwargs.enable_thinking` — verifiziert!) via `thinking_mode` (auto/never/always) + `thinking_budget`, **im Admin-UI einstellbar**. `auto` = Denken nur bei Onboarding/Identität → Alltag schnell (Wetter ~0,4 s, Uhrzeit ~0,3 s), Onboarding korrekt. Korrektur zur Nutzer-Quelle: min-p-Sampler beschleunigt das Denken NICHT (nur weniger Denk-Tokens helfen); `reasoning_effort` wirkungslos |
| 2026-06-18 | **Satelliten-Rückkanal + Geräteliste:** universelle `announce()` (Browser/Pi JSON+lokale TTS, ESP PCM-Streaming; Render-Capability `pcm`/`local` gegen Doppel-TTS). Heartbeat+Telemetrie (Raum/Lautstärke/RSSI/FW) von ESP **und** Pi → Admin-Tab 📡 Geräte (online/offline, last-seen). ESP-Firmware build-/flashbar aufgesetzt (VS Code/ESP-IDF; esp-sr/esp_codec_dev-API beim 1. Build ggf. anzupassen) |
| 2026-06-18 | **Autonomie / Automatisierungen** (`automations.py`): JARVIS plant & handelt selbstständig. Trigger zeit- (once/interval/daily/weekly) **und** ereignisgesteuert (`speaker_recognized`, erweiterbar via `/api/admin/events/fire`). Tools `create_/list_/cancel_automation`; autonomer Lauf = Agenten-Tool-Loop unter Besitzer-Rechten **+ Admin-Blacklist** (Tools/MCP) + Cooldown; „SILENT" unterdrückt Meldung; Ergebnis via `announce` an Zielquelle. Persistent (`automations.json`), Scheduler-Loop. Admin-UI-Tab 🤖. **End-to-End verifiziert:** Lauf (get_datetime autonom), Blacklist-Verweigerung, SILENT, Event-Auslösung |
| 2026-06-18 | **Ereignisquellen erweitert:** Register `KNOWN_EVENTS` + `emit()` an 10 Quellen verdrahtet (speaker_recognized, device_connected/disconnected, satellite_listening, timer_elapsed, user_created, voice_enrolled, document_uploaded, memory_saved, mcp_event). Externe Trigger via `POST /api/admin/events/fire`. Im create_automation-Tool + Admin-UI wählbar. In-code emit verifiziert |
| 2026-06-18 | **Code-Sandbox (Tier 3, `deploy/sandbox/`):** eigener gehärteter Container (Nicht-root, cap_drop ALL, mem/pids/cpu-Limits, setrlimit, Job-Timeout); Tools run_python/run_shell/list_/read_workspace_file; Internet pro Job per Admin-Toggle (`sandbox_allow_network`) via `unshare -rn` (seccomp=unconfined nötig); persistentes Workspace je Namespace; Rechte + Autonomie-Blacklist. Verifiziert: Agent wählte run_python autonom (Σ Quadrate 1..50 = 42925), Netz-AUS blockiert, Netz-AN erreichbar |
| 2026-06-18 | **fetch_url + Automatisierungs-Pflege:** Tool `fetch_url` (Seite laden, Titel/Überschriften/Lesetext via lxml, SSRF-Schutz) — zuverlässige News statt nur Snippets. Datums-Bewusstsein im Prompt (Chat+autonom) → korrekte Zeitberechnung; `once`-Vergangenheits-Guard. Browser-UI rendert `notify`. Admin: Automatisierungs-Prompts inline bearbeiten + „▶ Jetzt" zeigt Ergebnis. Sprachbefehl `update_automation` (owner-bezogen) korrigiert Aufgabentext. Auslieferung auch an verbundene Owner-Sessions. Verifiziert |
| 2026-06-18 | **Messaging-Kanal Telegram** (`messaging.py`): fester geräteunabhängiger Kanal — löst „Automatisierungs-Ergebnis kam nirgends an", wenn kein Browser-Tab offen. Ausgehend (Automatisierungen/Timer zusätzlich per Telegram an Besitzer; Tool `send_message` an aktuellen/benannten Nutzer) + eingehend (Long-Poll → `_run_chat` channel=telegram → Antwort). Pro-Nutzer `users.telegram_chat_id` (DB-Spalte + Admin-UI im Nutzer-Tab), Standard-Chat-ID als Fallback; Admin-Config (Token/Enable/Test) in System. `/start` liefert Chat-ID. Verifiziert (Zuordnung/Auflösung, disabled-No-op); Live-Versand benötigt Bot-Token vom Nutzer |
| 2026-06-18 | **Telegram-Sicherheitshärtung:** JARVIS sendet/antwortet AUSSCHLIESSLICH an verifizierte Chat-IDs (zugeordnet ODER Standard-Chat) — einziger Chokepoint `send_to_chat`→`is_verified`. Unverifizierte Eingänge → kein Agentenlauf/keine Antwort, nur Pending-Liste (Admin-UI Zuordnung). Live verifiziert: Bot `JarvisWS2026_bot`, fremder Chat 19447430 korrekt blockiert |
| 2026-06-18 | **Recherche-Agent + Vision-Tool:** `research` (Web-Suche → mehrere `fetch_url` → Synthese mit Quellen [n], verifiziert). Vision: `services.vision_call` (multimodal), Tool `analyze_image` (Bild-URL), `/api/vision`-Upload + Browser-📷. Erkenntnis: GPU-Server (192.168.66.225) hat KEIN Internet → externe Bild-URLs schlagen serverseitig fehl; Orchestrator lädt Bilder daher selbst und sendet base64-data-URI. Verifiziert (Python-Logo erkannt) |
| 2026-06-18 | **browser_control** (headless Chromium/Playwright in `deploy/sandbox`): Tools browse/browser_click/browser_type(+submit)/browser_screenshot(→Vision); persistente Browser-Sitzung je Namespace (Cookies/Logins). Sandbox-Image auf offizielle Playwright-Basis (mcr.microsoft.com/playwright/python) umgestellt (Debian-trixie `--with-deps` scheiterte an Ubuntu-Font-Paketen); Nutzer = pwuser(uid1001), Volume-Ownership-Fix. End-to-End verifiziert (example.com, Klick→iana.org, Wikipedia-Suche tippen+Enter, Screenshot-Beschreibung). **Phase 1.5 internes Toolset weitgehend komplett → als Nächstes Phase 2 (Client-Agent).** |
| 2026-06-18 | **Phase 2 gestartet (Protokoll-first):** Client-Agent-Protokoll + Capability-Registry + Routing. WS `/ws/client`; Hub: Capabilities je Session, Request/Response-Korrelation (`call_client`/`resolve_call`), Ziel-Auflösung. Tools `client_action`/`list_client_capabilities` (Capability-Gating + Rechte + Autonomie-Blacklist). Referenz-Thin-Client `deploy/client/jarvis-client.py` (app.launch/shell.run/window/media/fs/clipboard/system, Linux-Best-Effort, Win/mac-Zweige). End-to-End verifiziert: Agent → Client system.info (Linux 6.12) + shell.run echo. GUI/Tauri + Permission-UI als Nächstes |
| 2026-06-18 | **Client-Sicherheit (lokale Durchsetzung):** Policy-Engine im Thin-Client (`policy.json`: allow/ask/deny je Aktion, `fs_read/write_roots`-Scopes, `deny`-Liste). `gated_act` prüft VOR Ausführung; `ask` → Bestätigungsdialog (zenity/kdialog/osascript/MessageBox/Terminal), ohne Kanal **fail-safe deny**; lokales `audit.log` (Zeitstempel je Entscheidung). `deny`-Aktionen werden nicht als Capability gemeldet (+ server-seitiges Gating). Verifiziert: allow ausgeführt, ask ohne Kanal verweigert, deny nicht angeboten/serverseitig abgelehnt. Akzeptanzkriterium Phase 2 erfüllt (Client verweigert lokal, auch wenn Server sendet) |
| 2026-06-18 | **Client-Aktionen ausgebaut (31) + Pipeline:** Thin-Client um App-close, open.url/path, window close/min/max, screenshot, input.type/hotkey, notify, media.stop, volume.mute, system.suspend, fs.append/mkdir/delete erweitert (Linux-Best-Effort, Win/mac-Zweige). `client_screenshot`-Tool = `pipeline` (lokaler Screenshot → Vision-GPU). Policy-Defaults erweitert (anzeigen/lesen=allow, Wirkung=ask, fs.delete=deny). Verifiziert: erweiterte Capabilities gelistet, notify(allow) ausgeführt+auditiert, fs.delete(deny) nicht angeboten/abgelehnt. Desktop-abhängige Aktionen (screenshot/input/window/notify) brauchen GUI-Session am echten Client. Nächstes: Tauri-Desktop-App (Win/Linux/mac) |
| 2026-06-18 | **Desktop-App (Tauri v2, `deploy/desktop/`):** vollständiges Projekt — Rust-Tray + Sidecar-Supervisor (spawnt Python-Skript ODER PyInstaller-Binary, env: Server/Name/Policy/Audit/TLS/Shell), statisches Frontend (Einstellungen/Pairing, grafischer policy.json-Editor, Audit-Ansicht), `tauri.conf.json` + `capabilities/` + Icon-Quelle + Build-Anleitung Win/Linux/mac. JSON validiert; **hier nicht kompilierbar (keine Rust-Toolchain/GUI)** → Build/Feinschliff beim Nutzer (Icons via `cargo tauri icon`, erste `cargo tauri dev` ggf. kleine API-Anpassungen). Offen: Pairing/mTLS, Signierung, OS-Bundles erzeugen |
| 2026-06-18 | **Desktop-App auf Debian GEBAUT:** Toolchain hier installiert (Rust 1.96, tauri-cli 2.11.3, WebKit2GTK-4.1/AppIndicator-Deps). Icons via `cargo tauri icon` erzeugt. `cargo check` **fehlerfrei** (blind geschriebener Rust-Code kompiliert ohne Korrektur), `cargo tauri build --bundles deb` → **`SH-Jarvis_0.1.0_amd64.deb`** (3,1 MB; Depends: libayatana-appindicator3-1, libwebkit2gtk-4.1-0, libgtk-3-0; Binary usr/bin/sh-jarvis + .desktop). Win/.msi + mac/.dmg weiterhin nur nativ auf dem jeweiligen OS baubar (Projekt + Anleitung liegen vor) |
| 2026-06-18 | **Linux-Builds + Download-Bereich:** `cargo tauri build` erzeugte zusätzlich **AppImage** (Exit 0). Download-Center um Client-Pakete erweitert: Einträge Linux/Windows/macOS, ausgeliefert aus `deploy/desktop/dist/` (`/api/download/client/{platform}`), Linux-.deb bereits live. **Admin-Upload** `/api/admin/client-upload` (System-Tab) — Nutzer baut Win-.msi/mac-.dmg nativ und lädt es hoch → erscheint sofort unter /downloads (Typprüfung). Verifiziert: .deb-Download (3,18 MB, gültiges Paket), Upload+Ablehnung falscher Typen |
| 2026-06-18 | **Sidecar fest integriert:** `jarvis-client.py` wird als Tauri-Ressource gebündelt (`bundle.resources`), die App findet/startet ihn automatisch (kein manuelles Holen/Pfad-Eintragen mehr) — Auswahllogik: Override > mitgelieferte PyInstaller-Binary (python-frei) > mitgeliefertes .py + System-Python. Erststart-Fenster nur wenn config fehlt. Neu gebautes .deb enthält `usr/lib/SH-Jarvis/sidecar/jarvis-client.py` (verifiziert), dist aktualisiert. Gilt automatisch auch für Win-.msi/mac-.dmg-Builds |
| 2026-06-18 | **Client python-frei (Linux fertig, Windows vorbereitet):** Sidecar via PyInstaller zur eigenständigen Binary kompiliert (Interpreter + websockets eingebettet) und über Tauri **externalBin** ins Bundle gelegt (Triple-Namensschema `sidecar-bin/jarvis-client-<triple>`; App startet die Binary neben der Exe → KEIN Python am Zielrechner). Linux-.deb enthält jetzt `usr/bin/jarvis-client` (10 MB, kein .py mehr), standalone getestet, im Download-Bereich (13 MB). Windows: nur `…-pc-windows-msvc.exe` per PyInstaller auf Windows erzeugen, dann `cargo tauri build` (Anleitung im README). macOS vorerst nicht nötig |
| 2026-06-18 | **Client-Geräteadressierung + Windows-Fix:** Verbundene Client-Namen werden in den Prompt injiziert (`_connected_clients_hint`), neues Tool `list_clients`, `client_action(device=…)` adressiert gezielt (z.B. „Systeminfo vom Rechner VM"); bei Mehrdeutigkeit hilfreiche Rückfrage mit Namensliste (`_no_client_msg`). **Live verifiziert** gegen echten Windows-VM-Client des Nutzers (Routing korrekt). Windows-Konsolenfenster des Sidecars unterdrückt: `CREATE_NO_WINDOW` beim Spawn (main.rs) + `--noconsole` in build-sidecar.ps1 |
| 2026-06-19 | **Client-Aktionen: Windows-Lücken gefüllt + erweitert (37):** Windows jetzt voll abgedeckt über Bordmittel — Fenster (ctypes user32: EnumWindows/ShowWindow/SetForegroundWindow/WM_CLOSE), Eingabe (SendKeys), Medien/Lautstärke (VK-Tasten + Core-Audio Add-Type für absolute Lautstärke/Mute), Notify (Tray-Balloon), Zwischenablage (Get/Set-Clipboard); Subprozesse mit CREATE_NO_WINDOW (keine Konsolen-Popups). **Neue Aktionen:** volume.up/down, process.list, fs.move/copy, system.shutdown/restart (shutdown/restart+fs.delete default-deny). Linux-Pfade verifiziert (system.info/process.list/fs.copy/move/list); volume braucht Audio-HW. Windows-Code reviewt (hier nicht testbar). Linux-.deb mit neuen Aktionen neu gebaut |
| 2026-06-19 | **ESP32-S3-Satellit auf Hardware fertig:** Firmware gebaut/geflasht/getestet (Waveshare-Board). Mikrofon-Frontend auf esp-sr **AFE** umgestellt (NS/AGC/AFE-VAD), **Dual-Mic per `#define` umschaltbar**. Mehrere Iterationen an echten Logs gelöst: esp-sr-API (`get_feed_channel_num`/`VAD_SPEECH`); **FEED-Ringbuffer-Overflow** → getrennte feed/fetch-Tasks **und CPU 240 MHz** (160 nicht ausreichend für 2-Mic-BSS); **Lautstärke** → Software-Gain in `audio_write` (Codec-Volume griff nicht); **WS-Abbruch/Ruckeln bei TTS** → Uplink via eigenem Task entkoppelt, `WIFI_PS_NONE`, playback_task Prio 7, AFE-Pause während Wiedergabe, **getakteter TTS-Stream serverseitig**. Neu: **Remote Lautstärke + Mic-Gain pro Gerät über Admin-UI** (`/api/admin/devices/control`), Mic-Gain in NVS, Heartbeat meldet mic_gain. Firmware konsolidiert nach `deploy/satellite-esp/` (alte Variante entfernt), Download-Tar gehärtet (keine Build-Artefakte). **Akzeptanz Phase 3 erfüllt: ESP löst per Wake-Word vollständigen DE-Voice-Turn aus.** |
| 2026-06-19 | **Doku + GitHub-Veröffentlichung:** Projekt-`README.md` (GitHub) erstellt, ESP-README + `aktuellerstand.md` aktualisiert, `.gitignore` (schützt certs/config.json/automations.json/Build-Artefakte). Secret-Scan vor Push (keine echten Keys; certs/config ausgeschlossen). Hochgeladen nach **github.com/bmetallica/JARVIS** (public, MIT-LICENSE erhalten), 18 Topics gesetzt, Tag **v0.1.0** |
| 2026-06-20 | **Watcher-Automatisierungen** (`watchers.py` + `create_watch_automation`): effiziente Änderungs-Überwachung. Statt LLM-pro-Prüfung schreibt das LLM einmal ein Python-Prüfskript (Vertrag `state`+`emit()`), das pro Tick GÜNSTIG in der Code-Sandbox läuft; der LLM-Lauf wird nur bei echtem Treffer geweckt. Deterministische Dedup (`emit(False)` behält state), Test+Baseline vor dem Speichern, Netz-Override für Watcher-Skripte, **Self-Heal** (Agent repariert defektes Skript, sonst Pause+Meldung), Admin-UI 🔍-Badge/Zustand/Fehlerzähler. End-to-End gegen echte Sandbox getestet. Löst „behalte heise.de im Auge und melde neue Artikel" kostengünstig + zuverlässig |
| 2026-06-20 | **Selbst-gebaute Skills** (`skills.py` + Meta-Tools + Admin-UI 🧰): Jarvis baut sich wiederverwendbare, parametrisierte Werkzeuge aus Python-Code (`def run(args): return …`), die in der gehärteten Sandbox laufen — global, Admin kann editieren/deaktivieren/löschen. Test-before-save, Deferred-Katalog im Prompt (kein Schema-Bloat), Netz pro Skill. Tools create/run/search/describe/update/delete_skill. **Live verifiziert** (LLM baute+rief „addiere" → 10000). Phase 1+2 fertig; P3 typisierte Schema-Injektion + Self-Heal, P4 MCP-Expose offen |
| 2026-06-20 | **Skills Phase 3 — typisierte Deferred-Tools + Self-Heal:** `load_skills([namen])` lädt Skills on demand als getippte Werkzeuge `skill__<name>` (Tool-Loop + Stream-Loop bauen die Liste pro Iteration via `skills.schemas_for` neu); koexistiert mit untyped `run_skill`. Self-Heal-Nudge bei Skill-Fehler (→ update_skill). String-Coercion-Footgun im Vertrag behoben (defensives Casten). Standalone + Live verifiziert (skill__addiere: String-Args → 123, kein Concat). Offen nur noch P4 (optional): Skills als MCP-Server exponieren |
| 2026-06-20 | **Skills: erhöhte Rechte pro Tool (Admin-freigeschaltet).** Default-Skills laufen isoliert (kein LAN/Raw → „zu wenig Rechte" by design). Neu: opt-in **`sandbox-priv`**-Container (`network_mode: host` + `NET_RAW`, nur 127.0.0.1:8091 → nicht im LAN). Pro Skill `trust` (sandbox|elevated), **nur über Admin-UI 🧰 setzbar** (LLM kann nicht selbst eskalieren; Code-Änderung resettet trust→Re-Review); erhöhte Skills laufen **nicht autonom** außer `autonomous_ok`. Live verifiziert: Raw-Socket nur in elevated, Hostname=Host (im LAN), 8091 loopback-only, Autonom-Sperre greift |
| 2026-06-21 | **Skills: deklarierte Abhängigkeiten (statt Vorbacken) + Editieren gefixt.** Jarvis deklariert beim Erstellen IMMER die Requirements (`pip`/`apt`, im SKILL_CONTRACT erzwungen) → werden automatisch installiert (beliebige Pakete, Image bleibt schlank). `/install`-Endpoint (pip `--user` in Sandbox / system+apt in priv-root), persistentes Manifest + Re-Install beim Containerstart; pip wird beim Erstellen installiert (Wrapper hängt user-site an sys.path → importierbar trotz `python -I`); apt installiert app.py beim Erhöht-Freischalten in der priv-Spur. **Editieren:** catalog_hint nennt update/delete_skill; `syntax_ok` ohne test_args. Live: pip dnspython beim Erstellen, apt nmap bei Freischaltung, Edit klappt |
| 2026-06-21 | **Bild-Auslieferung (Telegram + Web-Chat).** Jarvis konnte erzeugte Diagramme nicht verschicken und halluzinierte „gesendet". Neu: Tool `send_image(path, caption)` — liest die Datei binär aus der Sandbox (`/file_b64`-Endpoint + `sandbox.read_bytes`; altes read_file zerstörte Binärdaten), liefert kanalabhängig: Telegram → `sendPhoto` (verifiziert), Web → Data-URI über `/ws` → `<img>` in app.js, Satellit → ehrliche Absage. `ctx['channel']` in _run_chat/chat_stream gesetzt. Standalone verifiziert |
| 2026-06-21 | **LAN-Zugriff für fetch_url (Admin-Toggle).** SSRF-Schutz blockte private IPs → Jarvis konnte Geräteseiten im Heimnetz (z.B. http://192.168.66.31:88) nicht lesen. Neu: Config `fetch_allow_lan` (Default aus), Admin-UI System→Netzwerkzugriff. `_url_is_safe` lässt private Adressen NUR bei Freigabe zu; Loopback/Link-Local/Reserved/Multicast bleiben IMMER gesperrt. Fehlermeldung weist auf die Admin-Freigabe hin. Verifiziert: 192.168.66.31 aus=blockiert/an=erlaubt, 127.0.0.1 immer blockiert |
| 2026-06-22 | **Agentik-Verbesserungen.** _TOOL_HINT: handeln statt ankündigen, nie „getan" behaupten ohne Tool, keine erfundenen Daten in Skills, GitHub-APIs aus raw.githubusercontent lesen. `browse`-Snapshot 4000→15000 Zeichen. `browser_click` matcht Text/Rolle/Title/Label/CSS (Icon-Buttons). |
| 2026-06-22 | **Denken bei Entwicklung erzwingen.** Adaptiver Modus blieb bei komplexer Agentenarbeit (Skills/UI) auf think=false → schwach. Fix: `_prepare_turn` erzwingt Denken (adaptive/auto), wenn Dev-Schlüsselwörter in der Anfrage (`_is_dev_request`) ODER Session im Dev-Flow (`hub.mark_dev`/`is_dev` ~15 min, gesetzt sobald create_skill/update_skill/run_python/browse… läuft). never/always unangetastet. Verifiziert |
| 2026-06-22 | **fetch_url liefert Quelltext vollständig.** Problem: fetch_url kürzte auf 4000 Zeichen + lxml-Artikel-Extraktion → das LLM sah von einer index.js nur den Anfang und riet API-Endpunkte (`/api/systems` → 404). Fix: Nicht-HTML-Inhalte (JS/JSON/Plaintext, z.B. raw.githubusercontent) werden ROH bis 50 000 Zeichen zurückgegeben. Verifiziert: volle index.js (50k) → alle echten Routen sichtbar (`/api/status`, `/api/update`, `/api/login`…). Nebenbefund: normaler Sandbox-Container erreicht LAN-Hosts per geroutetem HTTP (404 statt Conn-Fehler) → DPM-API-Skill braucht KEINE erhöhte Spur, nur net=true |
| 2026-06-22 | **Skill-Ergebnis-Bug bei großen Ausgaben.** Das Skill-Resultat wurde als JSON auf EINER stdout-Zeile ausgegeben → Sandbox kappt stdout bei 20k → Marker-Zeile mitten im JSON abgeschnitten → `_parse` scheitert → „Skill gab kein Ergebnis aus", obwohl es lief (LLM reparierte endlos an funktionierendem Code). Fix: `_wrap` schreibt das Ergebnis zusätzlich in `.skill_<token>.json`; `run_skill_code` liest es vollständig per `read_bytes` (stdout-Marker nur noch Fallback). Ergebnis an die KI auf 6000 Zeichen begrenzt (Hinweis „im Skill filtern"). Verifiziert: 300-System-Liste (~40k) → ok statt Abbruch |
| 2026-06-22 | **Degenerations-Schutz im Tool-Loop.** Modell (gemma) geriet in eine Schleife und rief get_datetime hunderte Male pro Antwort auf → minutenlanger Hänger + Retry-Verstärkung, Anfrage nie beantwortet. Fix: `_degenerate_calls` in _run_loop UND _stream_pass — >15 Tool-Calls in einer Antwort ODER dasselbe (Name+Args) >4× im Turn → sofortiger Abbruch mit klarer Meldung (ok=True → ausgeliefert, kein Retry). Verifiziert |
| 2026-06-23 | **Prompt-Caching aktiviert (`cache_prompt`).** Alle vier LLM-Aufrufe (`chat`/`vision_call`/`llm_call`/`llm_stream`) senden jetzt `cache_prompt` (Default true, Admin-UI-Checkbox, Config `llm_cache_prompt`). llama.cpp wiederverwendet so den KV-Cache des stabilen Präfix (System-Prompt) → schnellerer Prefill ohne neue Hardware, läuft im bestehenden „gedeckelten" Cache. Caveats: voller Nutzen nur bei byte-identischem Präfix (ändern sich Tools pro Turn, bricht der Cache ab dort); llama-swap-Modellwechsel leert den Cache |
| 2026-06-22 | **Tool-Auswahl-Stabilität (Bündel).** get_datetime-Tool ENTFERNT — es war der Ablenker, auf den gemma sich festbiss (Zeit steht ohnehin per _now_hint im Prompt); `frequency_penalty` (Default 0.3, Admin-UI) gegen Wiederholung; **502-Retry** in `_post_llm` (llama-swap-Ladefehler abfedern); Degenerations-Schutz entschärft → identische Calls werden entdoppelt+„jetzt antworten"-Nudge statt Hartabbruch, Abbruch nur bei >15 Calls/Antwort oder >12 unique → dann ok=False (adaptiv eskaliert auf Denken). **Erkenntnis:** gemma-12b wählt Tools nur MIT Denken zuverlässig (think=true 2/2 korrekt, think=false wählt oft falsches Tool/MCP). Empfehlung: thinking_mode=auto/always für Agenten-/Skill-Aufgaben. DPM-Skill `get_dpm_systems` auf pu>0-Filter umgebaut (kompaktes Ergebnis) |
