# SH-Jarvis — Raspberry-Pi-Satellit

Dünner Sprach-Client: lokales Wake-Word **„Jarvis"** → Aufnahme → Orchestrator (STT/LLM/TTS/Sprecher) → Lautsprecher.
Voll integriert: Timer-Alarme, Benachrichtigungen und Lautstärke kommen über den Rückkanal auf genau dieses Gerät.

## Voraussetzungen
- Raspberry Pi **3B+** (oder Pi 2), **64-bit Pi OS empfohlen** (für openWakeWord/onnxruntime).
- USB-Soundkarte mit Mikrofon + Lautsprecher (oder ReSpeaker-HAT).
- Erreichbarer Orchestrator (Tier 1), z. B. `https://192.168.66.224:8088`.

## Installation
```bash
tar xzf jarvis-satellite.tar.gz && cd satellite
sudo ./install.sh            # fragt Orchestrator-URL + Raumname ab
```
Der Installer richtet venv, Abhängigkeiten und den **systemd-Dienst** (Autostart) ein.

## Bedienung
- Sag **„Jarvis"**, warte auf den Wechsel zu „listening", dann sprich deine Anfrage.
- „**Jarvis, Lautstärke 7**" stellt die Lautstärke dieses Geräts (Stufe 1–10).
- Timer/Benachrichtigungen werden automatisch hier ausgesprochen.

## Konfiguration (`/opt/jarvis-satellite/satellite.conf`)
`orchestrator_url`, `room_name`, `wakeword` (`hey_jarvis`), `wakeword_threshold`,
`start_volume` (50), `input_device`/`output_device` (leer = ALSA-Default), `verify_tls`.

Audiogerät finden: `arecord -l` / `aplay -l` → Kartennummer in `input_device`/`output_device` eintragen.
Standard-ALSA-Gerät setzen: `~/.asoundrc`.

## Logs / Steuerung
```bash
journalctl -u jarvis-satellite -f
sudo systemctl restart jarvis-satellite
```

## Zero W
Auf dem **Zero W (ARMv6)** läuft openWakeWord nicht (keine onnx/tflite-Wheels). Dort stattdessen
**Porcupine** (`pvporcupine`, „Jarvis" eingebaut, kostenloser Picovoice-Key) oder **Vosk**.
Die Wake-Word-Erkennung in `satellite.py` ist dafür gekapselt (Backend-Tausch) — folgt bei der Zero-W-Portierung.

## Hinweise
- Selbstsigniertes Orchestrator-Cert: `verify_tls = false` (bis echtes Cert/Hostname vorliegt).
- Mehrere Satelliten: je eigener `room_name` → eigene Session, Alarme/Antworten am richtigen Ort.
