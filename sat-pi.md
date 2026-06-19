# Satellit — Raspberry Pi (sat-pi)

> Vollständiger Plan für einen Raspberry-Pi-Sprachsatelliten von SH-Jarvis.
> Ziel zuerst: **Pi 3B+** (vorhanden). Danach Portierung auf **Zero W**.
> Verwandt: [`sat-esp.md`](./sat-esp.md) (ESP32-Variante), [`aktuellerstand.md`](./aktuellerstand.md).

## 1. Idee
Der Satellit ist ein **dünner, headless Sprach-Client**: lokales Wake-Word, dann Audio aufnehmen und an
den Orchestrator schicken. STT, LLM, Tools, TTS, **Sprecher-Erkennung** laufen alle server-seitig
(Tier 1/2) — der Pi nutzt **dieselben Endpoints wie das Browser-UI**. Damit ist er „voll integriert":
Gedächtnis pro Sprecher, Rechte, Timer-Alarme an genau dieses Gerät usw.

## 2. Hardware
- **Pi 3B+** (ARM64, 4 Kerne, 1 GB) — Entwicklung/erster Test. **Zero W** (ARMv6, 512 MB) — Zielgerät später.
- **Audio:** USB-Soundkarte (Mikro-In + Lautsprecher-Out) oder ReSpeaker-HAT. ALSA.
- (Optional) WS2812-LED-Ring + Taster für Status/Mute — analog zur ESP-Variante; am Pi optional.

## 3. Wake-Word (plattformabhängig — austauschbar gekapselt)
Abstrakte Schnittstelle `WakeWord.detect()` mit drei Backends:
- **Pi 3B+/Pi 2:** **openWakeWord** (`hey_jarvis`-Modell, tflite/onnx) — **frei, offline, kein Account.** Empfohlen.
- **Zero W (ARMv6):** **Porcupine** (`pvporcupine`, „Jarvis" eingebaut, ARMv6-Binaries) — braucht kostenlosen Picovoice-AccessKey. ODER **Vosk**-Keyword-Spotting (frei/offline, CPU-hungriger).
- Auswahl per Config (`wakeword_engine = openwakeword|porcupine|vosk`).

## 4. Ablauf eines Turns
```
[idle] Wake-Word lauschen
  └─ "Jarvis" erkannt → kurzer Bestätigungston + LED „listening"
       └─ Aufnahme bis Stille (VAD: webrtcvad oder RMS-Schwelle, max. ~10 s)
            └─ POST /api/stt (multipart wav + session_id)  → {text, speaker}
                 └─ POST /api/chat/stream (session_id)      → SSE: Sätze
                      └─ je Satz: POST /api/tts → Audio in Wiedergabe-Queue (LED „speaking")
  └─ zurück zu [idle]
Parallel: WebSocket /ws offen → Push-Events (timer_alarm, set_volume, notify) jederzeit abspielen.
```
Barge-In: erkennt das Wake-Word während der Wiedergabe → Wiedergabe stoppen, neu zuhören.

## 5. Integration in den Agenten (Rückkanal)
Der Satellit registriert sich beim Start über `/ws` mit `client_type:"satellite"`, `name:"<Raumname>"`.
Damit ist er eine **Session mit Identität** — alle bestehenden Mechanismen greifen:
- **Timer-Alarme / Benachrichtigungen** kommen als WS-Push `{type:"timer_alarm"|"notify", message}` → Satellit spricht sie via TTS aus (genau auf diesem Gerät, weil dort erstellt).
- **Sprecher-Erkennung**: `/api/stt` liefert `speaker` → Gedächtnis/Rechte pro Person automatisch.
- **Lautstärke per Sprachbefehl** (siehe §6).

## 6. Lautstärke per Sprache (neuer Tool + Push)
Server-seitig **neues Tool `set_device_volume(level 1–10)`** (in `tools.py`): pusht
`{type:"set_volume", level}` an die **Ursprungs-Session** (das sprechende Gerät) über `session_hub.push`.
Der Satellit setzt daraufhin seine ALSA-/Software-Lautstärke. „Jarvis, Lautstärke 7" funktioniert damit
geräte-lokal und ist voll im Agenten verankert. (Gleicher Mechanismus wie beim ESP, §sat-esp.)
- Mapping Pi: Level 1–10 → ALSA-Mixer-Prozent (linear, z. B. 10–100 %); Default beim ersten Start 50 %.

## 7. Installation / Konfiguration (einfach)
**Tarball + `install.sh`** (kein Docker auf Zero W):
- `install.sh` (als `sudo`): apt-Abhängigkeiten (`python3-venv`, `libportaudio2`, `alsa-utils`, ggf. `ffmpeg`),
  venv anlegen, pip-Pakete (`requests`, `websocket-client`, `numpy`, `sounddevice`/`pyaudio`, `webrtcvad`,
  Wake-Word-Backend), Dateien nach `/opt/jarvis-satellite/`, **systemd-Dienst** `jarvis-satellite.service` (Autostart).
- **Interaktive Erstkonfiguration** (oder `satellite.conf`): Orchestrator-URL, Raumname, Wake-Word-Engine (+Key),
  Audio-Geräte (`arecord -l`/`aplay -l`), Startlautstärke.
- Selbstsigniertes TLS: Client akzeptiert das Cert (Cert mitliefern/pinnen, nicht generell „verify off").

### Dateien (`deploy/satellite/`)
- `satellite.py` (Hauptloop), `wakeword.py` (Backend-Abstraktion), `audio.py` (Aufnahme/Wiedergabe/VAD),
  `config.example`, `install.sh`, `jarvis-satellite.service`, `README.md`

## 8. Zustände & LEDs/Töne (optional am Pi, Pflicht am ESP)
IDLE · LISTENING · THINKING · SPEAKING · ERROR · SETUP(WLAN). Quittungstöne bei Wake/Done/Fehler.

## 9. Robustheit / „nicht vergessen"
- **Auto-Reconnect** des WS (Backoff), Offline-Puffer wie im Hub.
- **Heartbeat** an den Orchestrator → später im Admin-UI „welche Satelliten sind online".
- **NTP**-Zeit (für lokale Logs/Töne).
- **Mute-Taster**/Software-Mute; Datenschutz: keine Aufnahme vor Wake-Word.
- **Update**: `install.sh` re-runnable; Version in Config.
- **Mehrere Satelliten**: je eigener Raumname/Session → Alarme/Antworten landen am richtigen Ort.
- Fehlertoleranz: STT/LLM/Server nicht erreichbar → kurzer Fehlerton + LED „error", sauberer Rückfall in IDLE.

## 10. Reihenfolge der Umsetzung
1. `satellite.py` gegen die bestehenden Endpoints (Pi 3B+, openWakeWord). End-to-End: „Jarvis, wie spät ist es?".
2. `set_device_volume`-Tool + Push im Orchestrator; Lautstärke-Sprachbefehl.
3. WS-Push (Timer-Alarm) am Satelliten abspielen.
4. `install.sh` + systemd; Doku.
5. Portierung Zero W (Porcupine/Vosk), Performance prüfen.
