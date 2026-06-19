# Tier 2 — Inference (GPU-Server)

Alle GPU-Lasten von SH-Mark-XL an einem Ort. Deployst du auf deinem GPU-Server;
der Orchestrator (Tier 1) spricht die Dienste danach über das private Netz an.

## Dienste & Ports (aktueller Server 192.168.66.225)

| Dienst | Host-Port | Endpoint (OpenAI-kompatibel) | GPU | Quelle |
|--------|-----------|------------------------------|-----|--------|
| **LLM** (llama.cpp) | `8080` | `/v1/chat/completions` | ✅ | extern (nicht in dieser Compose) |
| STT (faster-whisper) | `8001` | `/v1/audio/transcriptions` | ✅ | `stt`-Service |
| TTS (deutscher Kokoro)| `8002` | `/v1/audio/speech` | ⬜ (CPU ok) | `tts`-Service |
| Ollama (optional, statt llama.cpp) | `11434` | `/api/chat` | ✅ | `--profile ollama` |

> LLM läuft hier als **eigenständiger llama.cpp-Server** auf :8080. Der Ollama-Dienst
> dieser Compose ist daher **optional** und standardmäßig deaktiviert. Diese Compose
> startet per Default nur **STT (8001)** und **TTS (8002)**.

## Setup

```bash
# 1. Voraussetzungen prüfen
nvidia-smi                      # Treiber ok?
docker info | grep -i runtime   # nvidia-runtime vorhanden?

# 2. Konfig anlegen
cp .env.example .env            # ggf. BIND_ADDR / Modelle anpassen

# 3. Dienste starten (Default: STT + TTS; LLM = externer llama.cpp)
docker compose -f docker-compose.gpu.yml up -d

# 4. (optional) Statt llama.cpp Ollama nutzen + Modelle laden:
# docker compose -f docker-compose.gpu.yml --profile ollama up -d ollama
# docker compose -f docker-compose.gpu.yml --profile ollama --profile init up ollama-init

# Status / Logs
docker compose -f docker-compose.gpu.yml ps
docker compose -f docker-compose.gpu.yml logs -f
```

## Schnelltest (vom Orchestrator oder lokal)

```bash
# Ersetze GPU_IP durch die Adresse des Servers
GPU_IP=192.168.x.x

# LLM (llama.cpp)
curl http://$GPU_IP:8080/v1/models

# STT (kurze WAV-Datei)
curl -F "file=@test_de.wav" -F "language=de" \
     http://$GPU_IP:8001/v1/audio/transcriptions

# TTS
curl -X POST http://$GPU_IP:8002/v1/audio/speech \
     -H "Content-Type: application/json" \
     -d '{"model":"kokoro","voice":"dm_martin","input":"Hallo, ich bin Mark."}' \
     --output test_out.wav
```

## ⚠️ Sicherheit
Keiner dieser Dienste hat eigene Authentifizierung. **Nicht** direkt ins Internet
exposen. Betrieb nur im LAN oder über ein privates VPN (WireGuard/Tailscale).
`BIND_ADDR` in `.env` möglichst auf die VPN-Adresse einschränken.

## Was der Orchestrator/Client später braucht
Gib mir nach dem Deploy diese Werte — daraus konfiguriere ich Tier 1 / den Client:
- **GPU-Server-IP** (bzw. VPN-Hostname)
- bestätigte Ports (Standard: `11434` / `8001` / `8080`)
- tatsächlich geladenes **LLM-/Vision-Modell** (falls von Defaults abweichend)
