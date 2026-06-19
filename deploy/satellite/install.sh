#!/usr/bin/env bash
# SH-Jarvis Satellit — Installer für Raspberry Pi (Pi 3B+/Pi 2; 64-bit Pi OS empfohlen).
# Aufruf:  sudo ./install.sh
set -e

DEST=/opt/jarvis-satellite
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "== SH-Jarvis Satellit Installer =="
if [ "$(id -u)" -ne 0 ]; then echo "Bitte mit sudo ausführen."; exit 1; fi

echo "-- System-Pakete --"
apt-get update -q
apt-get install -y -q python3-venv python3-dev libportaudio2 portaudio19-dev \
                      ffmpeg alsa-utils libatlas-base-dev

echo "-- Dateien nach $DEST --"
mkdir -p "$DEST"
cp -f "$SRC/satellite.py" "$DEST/"
[ -f "$DEST/satellite.conf" ] || cp -f "$SRC/config.example" "$DEST/satellite.conf"

echo "-- Python-venv + Pakete --"
python3 -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --upgrade pip -q
"$DEST/venv/bin/pip" install -q requests websocket-client sounddevice webrtcvad openwakeword
# tflite_runtime u.a. sind gegen NumPy 1.x gebaut → NumPy 2 bricht sie. Daher pinnen:
"$DEST/venv/bin/pip" install -q "numpy<2"

echo "-- openWakeWord-Modelle laden (hey_jarvis + Feature-Modelle) --"
"$DEST/venv/bin/python" -c "import openwakeword.utils; openwakeword.utils.download_models()" \
  || echo "WARN: Modell-Download fehlgeschlagen — wird beim ersten Start erneut versucht."

# -- Interaktive Konfiguration (nur wenn noch nicht gesetzt) --
CONF="$DEST/satellite.conf"
CUR_URL=$(grep -oP 'orchestrator_url\s*=\s*\K.*' "$CONF" || true)
CUR_ROOM=$(grep -oP 'room_name\s*=\s*\K.*' "$CONF" || true)
read -rp "Orchestrator-URL [$CUR_URL]: " URL || true
read -rp "Raumname [$CUR_ROOM]: " ROOM || true
[ -n "$URL" ]  && sed -i "s|^orchestrator_url.*|orchestrator_url = $URL|" "$CONF"
[ -n "$ROOM" ] && sed -i "s|^room_name.*|room_name = $ROOM|" "$CONF"

echo "-- Audiogeräte (zur Info) --"; arecord -l 2>/dev/null | grep -i card || true; aplay -l 2>/dev/null | grep -i card || true
echo "   (Bei Bedarf input_device/output_device in $CONF setzen.)"

echo "-- systemd-Dienst --"
cp -f "$SRC/jarvis-satellite.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable jarvis-satellite.service
systemctl restart jarvis-satellite.service

echo "== Fertig. Logs:  journalctl -u jarvis-satellite -f =="
echo "   Sag 'Jarvis', dann deine Frage."
