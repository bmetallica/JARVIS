#!/usr/bin/env bash
# Baut den Sidecar als eigenständige Binary (PyInstaller) und legt sie für den Tauri-Build ab.
# Auf dem jeweiligen OS ausführen (die Binary ist nativ):  ./build-sidecar.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "→ Installiere Build-Abhängigkeiten (pyinstaller, websockets)…"
python3 -m pip install --quiet --break-system-packages pyinstaller websockets 2>/dev/null \
  || python3 -m pip install --quiet pyinstaller websockets
python3 -m pip uninstall -y typing >/dev/null 2>&1 || true   # obsoletes Backport stört PyInstaller

echo "→ Baue Standalone-Binary…"
rm -rf build dist ./*.spec
pyinstaller --onefile --name jarvis-client --clean jarvis-client.py >/dev/null

triple=$(rustc -vV | awk '/^host:/{print $2}')
dest="../desktop/src-tauri/sidecar-bin"
mkdir -p "$dest"
cp dist/jarvis-client "$dest/jarvis-client-$triple"
chmod +x "$dest/jarvis-client-$triple"

echo "✓ Fertig: sidecar-bin/jarvis-client-$triple"
echo "  Jetzt:  cd ../desktop/src-tauri && cargo tauri icon icons/icon.png && cargo tauri build"
