# Tier 1 — Orchestrator (JARVIS)

Das zentrale „Gehirn" (CPU-leichtgewichtig). Bündelt den GPU-Inferenz-Tier
(LLM/STT/TTS) und stellt ein Web-UI mit Chat- und Voice-Grundfunktionen bereit.

## Start

```bash
cd orchestrator
python3 -m pip install -r requirements.txt   # einmalig
./run.sh                                       # → http://<host>:8088
```

UI im Browser öffnen: **https://192.168.66.224:8088**
(Port 8088, da 8000 auf diesem Host von einem anderen Container belegt ist.)

> **HTTPS ist nötig fürs Browser-Mikrofon** (`getUserMedia` gibt es nur im
> Secure Context). `run.sh` startet automatisch mit TLS, wenn `certs/` existiert;
> Cert erzeugen mit `./gen_cert.sh [IP]`. Da es selbstsigniert ist, zeigt der
> Browser einmalig eine Warnung → „Erweitert" → „Trotzdem fortfahren". Danach
> funktioniert das Mikrofon. Chat/Tippen geht ohnehin immer.

## Endpoints

| Methode | Pfad | Zweck |
|---------|------|-------|
| GET  | `/` | Web-UI |
| GET  | `/health` | Orchestrator + Erreichbarkeit der 3 GPU-Dienste |
| GET  | `/api/config` · POST | Endpoints/Modelle lesen / ändern (persistiert in `config.json`) |
| GET  | `/api/models` | verfügbare LLM-Modelle (vom llama.cpp-Server) |
| POST | `/api/chat` | `{message, history}` → `{reply}` (Deutsch, gemma4) |
| POST | `/api/stt` | Audio-Datei (multipart `file`) → `{text}` |
| POST | `/api/tts` | `{text}` → `audio/wav` |

## Konfiguration (`config.json`)
LLM/STT/TTS-URLs + Modelle + System-Prompt. Zur Laufzeit über das ⚙-Panel
im UI änderbar (erfüllt den Wunsch „Endpoints über UI einstellbar").

Aktuell: LLM `gemma4-26b` @ `:8080` · STT @ `:8001` · TTS `martin` @ `:8002`
(alle auf dem GPU-Server 192.168.66.225).

## TTS-Engines (im ⚙-Panel umschaltbar)
| Engine | Ort | Tempo | Hinweis |
|--------|-----|-------|---------|
| **edge** (Default) | Cloud (Microsoft) | ~0.6–1.3 s | natürlichste DE-Stimme (`de-DE-ConradNeural`), braucht Internet |
| **piper** | lokal CPU (offline) | ~0.1 s (warm) | `de_DE-thorsten-medium`; Stimme lädt einmalig nach `piper_voices/` |
| **kokoro** | GPU-Server (offline) | ~2–7 s | dt. Kokoro-Container auf :8002 |

Je Engine eine eigene Stimme (`tts_voice_edge` / `tts_voice_piper` / `tts_voice_kokoro`),
damit das Umschalten nichts überschreibt.

## Hinweise
- **gemma4-12b ist ein Reasoning-Modell** → liefert `reasoning_content` separat;
  der Orchestrator nutzt nur `content`. `llm_max_tokens` großzügig (512).
- LLM-Latenz ist v.a. **llama-swap-Modellwechsel** — aktives Modell pinnen; warm ~6 s.
- Schnellere Alternative ohne Reasoning: `Qwen2.5-Omni` / `qwen2.5-7b` (⚙-Panel).
