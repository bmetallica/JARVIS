#!/usr/bin/env python3
"""
SH-Jarvis — Raspberry-Pi-Sprachsatellit (dünner Client).

Lokales Wake-Word „Jarvis" (openWakeWord) → Aufnahme bis Stille → an den Orchestrator:
  STT (/api/stt, inkl. Sprecher-Erkennung) → Chat (/api/chat/stream) → TTS (/api/tts) → Lautsprecher.
Eine WebSocket-Verbindung (/ws) hält den Rückkanal offen: Timer-Alarme, Benachrichtigungen,
Lautstärke (set_volume) werden auf GENAU diesem Gerät ausgeführt.

Getestet als Thin-Client gegen den bestehenden Orchestrator. Audio-Hardware: USB-Soundkarte (ALSA).
Konfiguration: satellite.conf  (siehe config.example).
"""
from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave

import numpy as np
import requests
import sounddevice as sd
import webrtcvad
import websocket          # websocket-client
import urllib3
urllib3.disable_warnings()

# ── Konfiguration ─────────────────────────────────────────────────────────────
CONF_PATH = os.environ.get("JARVIS_SAT_CONF", os.path.join(os.path.dirname(__file__), "satellite.conf"))


def load_conf() -> dict:
    cfg = {
        "orchestrator_url": "https://192.168.66.224:8088",
        "room_name": "Satellit",
        "wakeword": "hey_jarvis",
        "wakeword_threshold": "0.5",
        "start_volume": "50",
        "max_volume": "100",          # Pi: kein Verstärker-Cap nötig (ESP cappt bei 90)
        "verify_tls": "false",
        "input_device": "",
        "output_device": "",
    }
    try:
        for line in open(CONF_PATH, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg


CFG = load_conf()
BASE = CFG["orchestrator_url"].rstrip("/")
VERIFY = CFG["verify_tls"].lower() == "true"
ROOM = CFG["room_name"]
SAMPLE_RATE = 16000

# stabile Session-ID (persistiert) — für WS-Hello UND HTTP, damit Pushes ankommen
_SID_FILE = os.path.join(os.path.dirname(__file__), ".session_id")
try:
    SESSION_ID = open(_SID_FILE).read().strip() or uuid.uuid4().hex[:12]
except FileNotFoundError:
    SESSION_ID = uuid.uuid4().hex[:12]
open(_SID_FILE, "w").write(SESSION_ID)

current_volume = int(CFG["start_volume"])        # 0–100 (Pi)
MAX_VOL = int(CFG["max_volume"])
busy_speaking = threading.Event()

FW_VERSION = "1.0.0-pi"
HEARTBEAT_S = 15                                  # Lebenszeichen-Intervall → Admin-Geräteliste
WS = None                                         # aktuelle WebSocketApp (für Heartbeat-Versand)


def log(*a):
    print(f"[sat {time.strftime('%H:%M:%S')}]", *a, flush=True)


# ── Lautstärke über ALSA-Hardware-Mixer (zuverlässig bei USB-Karten) ───────────
ALSA_CARD = CFG.get("alsa_card", "").strip()
USE_AMIXER = False
MIXER_CTRL = None


def _amixer_base():
    return ["amixer"] + (["-c", ALSA_CARD] if ALSA_CARD else [])


def detect_mixer():
    global USE_AMIXER, MIXER_CTRL
    import re
    try:
        out = subprocess.run(_amixer_base() + ["scontrols"], capture_output=True, text=True, timeout=5).stdout
        ctrls = re.findall(r"'([^']+)'", out)
        for pref in ("PCM", "Speaker", "Master", "Headphone", "Playback"):
            if pref in ctrls:
                MIXER_CTRL = pref; break
        else:
            MIXER_CTRL = ctrls[0] if ctrls else None
        USE_AMIXER = MIXER_CTRL is not None
        log("ALSA-Mixer:", MIXER_CTRL if USE_AMIXER else "keiner gefunden → Software-Volume")
    except Exception as e:
        log("amixer nicht verfügbar:", e)


def alsa_set_volume(pct: int) -> bool:
    if not USE_AMIXER:
        return False
    try:
        # -M = gehörrichtige (gemappte) Lautstärke; nötig für den Pi-Onboard-Ausgang (bcm2835),
        # dessen rohe dB-Spanne sonst dafür sorgt, dass Prozente kaum wirken.
        r = subprocess.run(_amixer_base() + ["-M", "set", MIXER_CTRL, f"{pct}%"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            log("amixer set fehlgeschlagen:", (r.stderr or r.stdout).strip()[:160])
            return False
        return True
    except Exception as e:
        log("amixer set Fehler:", e); return False


# ── LED (am Pi optional — Log-Stub, später GPIO/WS2812) ────────────────────────
def set_led(state: str):
    log("LED:", state)


# ── Signaltöne (wie Alexa: aufsteigend = „höre zu", absteigend = „fertig") ─────
def _gen_tone(freqs, dur=0.12, gap=0.03):
    parts = []
    for f in freqs:
        t = np.linspace(0, dur, int(SAMPLE_RATE * dur), False)
        wave = np.sin(2 * np.pi * f * t)
        n = len(t)
        env = np.minimum(np.linspace(0, 1, n) * 8, np.minimum(1.0, np.linspace(1, 0, n) * 8))  # weiche Flanken
        parts.append((wave * env).astype(np.float32))
        parts.append(np.zeros(int(SAMPLE_RATE * gap), dtype=np.float32))
    return np.concatenate(parts)


START_TONE = _gen_tone([660, 990])    # aufsteigend → „ich höre zu"
END_TONE = _gen_tone([880], dur=0.10) # kurzer Ton → „fertig zugehört"


def play_tone(tone):
    try:
        sd.play(tone * 0.5, SAMPLE_RATE)   # fester Pegel — Lautstärke regelt der Hardware-Mixer
        sd.wait()
    except Exception as e:
        log("Ton:", e)


# ── Audio: Wiedergabe via ffplay (mp3/wav), Lautstärke 0–100 ───────────────────
def play_audio(data: bytes):
    """TTS-Audio (mp3/wav) via ffmpeg zu float32-PCM dekodieren, per Software auf die
    aktuelle Lautstärke skalieren und über sounddevice (ALSA-Default = Kopfhörer) ausgeben."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-loglevel", "quiet", "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", "24000", "pipe:1"],
            input=data, capture_output=True)
        pcm = np.frombuffer(proc.stdout, dtype=np.float32)
        if pcm.size == 0:
            log("Audio-Dekodierung leer (ffmpeg)"); return
        sd.play(pcm, 24000)            # voller Pegel — Lautstärke regelt der Hardware-Mixer (amixer)
        sd.wait()
    except Exception as e:
        log("Wiedergabe-Fehler:", e)


def tts_and_play(text: str):
    if not text.strip():
        return
    try:
        r = requests.post(f"{BASE}/api/tts", json={"text": text}, verify=VERIFY, timeout=60)
        if r.ok:
            play_audio(r.content)
    except Exception as e:
        log("TTS-Fehler:", e)


# ── WAV-Helfer ─────────────────────────────────────────────────────────────────
def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


# ── Aufnahme bis Stille (VAD) ──────────────────────────────────────────────────
def record_until_silence(stream) -> bytes:
    vad = webrtcvad.Vad(2)
    frames, silence_ms, started = [], 0, False
    for _ in range(int(10000 / 30)):            # max ~10 s
        data, _ = stream.read(480)              # 30 ms @16k
        b = data[:, 0].tobytes() if data.ndim > 1 else data.tobytes()
        frames.append(b)
        if vad.is_speech(b, SAMPLE_RATE):
            started = True; silence_ms = 0
        elif started:
            silence_ms += 30
            if silence_ms > 800:                # 0,8 s Stille → Ende
                break
    return b"".join(frames)


# ── Ein Turn: Wake → Aufnahme → STT → Chat (stream) → TTS ──────────────────────
def handle_utterance(stream):
    busy_speaking.set()
    try:
        set_led("listening")
        play_tone(START_TONE)                    # „ich höre zu" (wie Alexa)
        pcm = record_until_silence(stream)
        play_tone(END_TONE)                      # „fertig zugehört"
        if len(pcm) < SAMPLE_RATE:              # < 0,5 s → ignorieren
            set_led("idle"); return
        set_led("thinking")
        # STT
        try:
            files = {"file": ("speech.wav", pcm_to_wav(pcm), "audio/wav")}
            r = requests.post(f"{BASE}/api/stt", files=files,
                              data={"session_id": SESSION_ID}, verify=VERIFY, timeout=60)
            text = r.json().get("text", "").strip()
            spk = (r.json().get("speaker") or {}).get("username")
            log("STT:", repr(text), "| Sprecher:", spk)
        except Exception as e:
            log("STT-Fehler:", e); set_led("error"); time.sleep(1); set_led("idle"); return
        if not text:
            set_led("idle"); return
        # Chat streamen → satzweise TTS
        set_led("speaking")
        try:
            with requests.post(f"{BASE}/api/chat/stream",
                               json={"message": text, "session_id": SESSION_ID},
                               verify=VERIFY, timeout=200, stream=True) as resp:
                ev = ""
                for raw in resp.iter_lines():
                    if not raw:
                        continue
                    s = raw.decode("utf-8", "replace")
                    if s.startswith("event:"):
                        ev = s[6:].strip()
                    elif s.startswith("data:"):
                        try:
                            payload = json.loads(s[5:].strip())
                        except Exception:
                            continue
                        if ev == "sentence":
                            tts_and_play(payload.get("text", ""))
                        elif ev == "error":
                            log("Chat-Fehler:", payload.get("detail"))
        except Exception as e:
            log("Chat-Fehler:", e); set_led("error"); time.sleep(1)
        set_led("idle")
    finally:
        busy_speaking.clear()


# ── WebSocket-Rückkanal (Timer/Benachrichtigung/Lautstärke) ────────────────────
def on_ws_message(ws, message):
    global current_volume
    try:
        ev = json.loads(message)
    except Exception:
        return
    t = ev.get("type")
    if t == "welcome":
        log("WS verbunden, Session:", ev.get("session_id"))
    elif t == "set_volume":
        level = int(ev.get("level", 5))
        current_volume = max(0, min(level * 10, MAX_VOL))   # 1–10 → 10–100 %
        ok = alsa_set_volume(current_volume)                 # echte Hardware-Lautstärke (amixer)
        log("Lautstärke →", current_volume, "% (HW)" if ok else "% (amixer wirkungslos!)")
        threading.Thread(target=tts_and_play, args=(f"Lautstärke {level}.",), daemon=True).start()
    elif t in ("timer_alarm", "notify"):
        msg = ev.get("message", "")
        log("Push:", t, msg)
        threading.Thread(target=tts_and_play, args=(msg,), daemon=True).start()


def wifi_rssi():
    """WLAN-Signal in dBm (oder None bei LAN/unbekannt) — aus /proc/net/wireless."""
    try:
        with open("/proc/net/wireless") as f:
            for line in f.readlines()[2:]:
                parts = line.split()
                if len(parts) >= 4:
                    return int(float(parts[3].rstrip(".")))   # Spalte „level"
    except Exception:
        pass
    return None


def _ws_hello(w):
    global WS
    WS = w
    w.send(json.dumps({"type": "hello", "client_type": "satellite", "name": ROOM,
                       "session_id": SESSION_ID, "volume": current_volume, "fw": FW_VERSION}))


def heartbeat_thread():
    """Periodisches Lebenszeichen inkl. Telemetrie → Admin-Geräteliste."""
    while True:
        time.sleep(HEARTBEAT_S)
        if WS is not None:
            try:
                WS.send(json.dumps({"type": "heartbeat", "room": ROOM, "volume": current_volume,
                                    "rssi": wifi_rssi(), "fw": FW_VERSION}))
            except Exception:
                pass                                       # getrennt → ws_thread reconnectet


def ws_thread():
    global WS
    url = BASE.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
    sslopt = {"cert_reqs": 0} if not VERIFY else None     # selbstsigniertes Cert akzeptieren
    while True:
        try:
            ws = websocket.WebSocketApp(
                url, on_message=on_ws_message, on_open=_ws_hello,
                on_close=lambda w, *a: globals().__setitem__("WS", None),
            )
            ws.run_forever(sslopt=sslopt, ping_interval=30)
        except Exception as e:
            log("WS-Fehler:", e)
        WS = None
        time.sleep(3)                                     # Reconnect-Backoff


# ── Hauptloop: Wake-Word ───────────────────────────────────────────────────────
def main():
    log(f"Starte Satellit {ROOM} -> {BASE} (Session {SESSION_ID})")
    detect_mixer()
    alsa_set_volume(current_volume)                  # Startlautstärke (Hardware) anwenden
    threading.Thread(target=ws_thread, daemon=True).start()
    threading.Thread(target=heartbeat_thread, daemon=True).start()

    import openwakeword.utils
    from openwakeword.model import Model
    try:
        openwakeword.utils.download_models()          # lädt Feature- + Wake-Word-Modelle (idempotent)
    except Exception as e:
        log("openWakeWord-Modell-Download:", e)
    oww = Model(wakeword_models=[CFG["wakeword"]])
    thr = float(CFG["wakeword_threshold"])
    key = CFG["wakeword"]

    # Verfügbare Eingabegeräte loggen (Hilfe bei der Konfiguration)
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                log(f"  Audio-Eingang [{i}] {d['name']}")
    except Exception as e:
        log("Geräteliste:", e)

    def _dev(v):
        v = (v or "").strip()
        return (int(v) if v.isdigit() else v) if v else None

    in_dev = _dev(CFG["input_device"])
    out_dev = _dev(CFG["output_device"])
    if out_dev is not None:
        sd.default.device = (in_dev, out_dev)        # Töne/Wiedergabe folgen dem Ausgabegerät
    try:
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=in_dev)
        stream.start()
    except Exception as e:
        log(f"Eingabegerät {in_dev!r} nicht nutzbar ({e}) → Standardgerät. "
            "Tipp: PortAudio-Index/Namen verwenden (NICHT 'plughw:...'); siehe Liste oben.")
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16")
        stream.start()
    set_led("idle")
    log("Bereit - sage 'Jarvis'.")
    try:
        while True:
            data, _ = stream.read(1280)                   # 80 ms
            frame = data[:, 0] if data.ndim > 1 else data
            scores = oww.predict(frame)
            score = scores.get(key, max(scores.values()) if scores else 0.0)
            if score >= thr and not busy_speaking.is_set():
                log(f"Wake-Word erkannt ({score:.2f})")
                handle_utterance(stream)
                oww.reset()
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop(); stream.close()


if __name__ == "__main__":
    main()
