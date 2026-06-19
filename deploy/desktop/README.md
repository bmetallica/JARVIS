# SH-Jarvis Desktop-App (Tauri v2 · Windows / Linux / macOS)

Schlanke Desktop-Hülle um den **Python-Sidecar** (`deploy/client/jarvis-client.py`):
System-Tray, Einstellungen/Pairing, **grafische Berechtigungs-Verwaltung** (`policy.json`) und
Protokoll-Ansicht. Die App startet und überwacht den Sidecar; die eigentliche Logik (WS-Verbindung,
Aktionen, lokale Sicherheits-Policy) bleibt im Sidecar.

> **Status:** Das **Linux-Paket (`.deb`) wurde gebaut & getestet** (python-frei, Sidecar-Binary eingebettet)
> und liegt im Download-Bereich. **Windows** baust du auf einem Windows-Rechner (Schritte unten —
> ein Skript erledigt den Sidecar). **macOS** analog, derzeit nicht erforderlich.

## Architektur
```
Tauri-App (Rust, Tray + GUI)
   ├─ verwaltet config.json + policy.json (im App-Config-Verzeichnis)
   └─ startet/überwacht  →  Python-Sidecar (jarvis-client.py)  →  WS /ws/client → Orchestrator
```
Config-Verzeichnis (automatisch): Linux `~/.config/de.shjarvis.client/`,
Windows `%APPDATA%\de.shjarvis.client\`, macOS `~/Library/Application Support/de.shjarvis.client/`.

## Voraussetzungen
- **Rust** (https://rustup.rs) + **Tauri-CLI**: `cargo install tauri-cli --version "^2"`
- **OS-Build-Abhängigkeiten:**
  - **Linux:** `webkit2gtk-4.1`, `libayatana-appindicator3`, `librsvg2`, `build-essential`, `curl`, `wget`, `file`, `libssl-dev`
    (Debian/Ubuntu: `sudo apt install libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev build-essential curl wget file libssl-dev`)
  - **Windows:** *Microsoft C++ Build Tools* + *WebView2 Runtime* (auf Win11 vorhanden)
  - **macOS:** *Xcode Command Line Tools* (`xcode-select --install`)
- **Zum BAUEN** des Sidecars: Python 3 + PyInstaller (`pip install pyinstaller websockets`).
  **Am Zielrechner ist KEIN Python nötig** — die eigenständige Sidecar-Binary ist eingebettet (`externalBin`).

## 1× Setup: Icons erzeugen
Tauri braucht plattform-spezifische Icons. Aus dem mitgelieferten `icons/icon.png` generieren:
```bash
cd deploy/desktop/src-tauri
cargo tauri icon icons/icon.png      # erzeugt 32x32.png, 128x128.png, icon.icns, icon.ico …
```
(Eigenes Logo? `icon.png` (≥512×512) ersetzen und Befehl erneut ausführen.)

## Entwickeln / Starten
```bash
cd deploy/desktop/src-tauri
cargo tauri dev
```
Der **Sidecar ist als eigenständige Binary in der App enthalten** (`externalBin`, siehe unten) und
startet **automatisch** — nichts manuell besorgen oder eintragen, **kein Python am Zielrechner**.
Beim Erststart öffnet sich das Fenster:
1. **Einstellungen:** Orchestrator-URL, Gerätename, „Shell erlauben"/„TLS prüfen" → *Speichern*.
2. **Berechtigungen:** `policy.json` bearbeiten (allow/ask/deny je Aktion, Pfad-Scopes) → *Speichern*.
3. Fenster schließen = App läuft im **Tray** weiter (Öffnen / Sidecar neu starten / Beenden).

> Hinweis für `cargo tauri dev`/`build`: Die Sidecar-Binary muss vorab in `src-tauri/sidecar-bin/`
> liegen (siehe „Python-frei" unten / `build-sidecar`-Skript), sonst bricht der Build ab.
> Die Felder „Python-Befehl"/„Sidecar-Pfad" im UI sind nur ein **optionaler Override**.

## Bauen (Release) je OS
Jeweils auf dem Ziel-OS (Cross-Compiling vermeiden):
```bash
cd deploy/desktop/src-tauri
cargo tauri build
```
Ergebnisse unter `target/release/bundle/`:
- **Linux:** `appimage/*.AppImage`, `deb/*.deb`
- **Windows:** `msi/*.msi`, `nsis/*-setup.exe`
- **macOS:** `macos/*.app`, `dmg/*.dmg` (Signierung/Notarisierung separat mit Apple-ID)

## Python-frei: eingebettete Sidecar-Binary (Standard)
Der Sidecar wird als **eigenständige Binary** (PyInstaller, Interpreter + `websockets` eingebettet) über
Tauris **`externalBin`** mitgeliefert — am Zielrechner ist **kein Python nötig**. Tauri sucht je
Plattform die Datei mit dem passenden Rust-Target-Triple in `src-tauri/sidecar-bin/` und legt sie zur
Laufzeit neben die App-Exe; die App startet sie automatisch.

Benötigte Datei je OS (vor `cargo tauri build` erzeugen — **auf dem jeweiligen OS**, da die Binary nativ ist):
| OS | Target-Triple ermitteln | Erwartete Datei in `src-tauri/sidecar-bin/` |
|----|------------------------|---------------------------------------------|
| Linux  | `rustc -vV` → host | `jarvis-client-x86_64-unknown-linux-gnu` |
| Windows | `rustc -vV` → host | `jarvis-client-x86_64-pc-windows-msvc.exe` |

So baust du die Binary (auf dem jeweiligen OS):
```bash
cd deploy/client
pip install pyinstaller websockets        # nur zum BAUEN; Endnutzer braucht es nicht
pyinstaller --onefile --name jarvis-client jarvis-client.py
# Ergebnis dist/jarvis-client  →  passend benennen und ablegen:
#   Linux:   cp dist/jarvis-client       ../desktop/src-tauri/sidecar-bin/jarvis-client-x86_64-unknown-linux-gnu
#   Windows: copy dist\jarvis-client.exe ..\desktop\src-tauri\sidecar-bin\jarvis-client-x86_64-pc-windows-msvc.exe
```
Danach `cargo tauri build` → das Paket enthält die Binary; Start ist **vollständig ohne Python**.

> **Komfort:** Statt der manuellen Schritte einfach `deploy/client/build-sidecar.sh` (Linux/macOS) bzw. `build-sidecar.ps1` (Windows) ausführen — erkennt das Triple, baut und legt die Binary korrekt ab.
*(Der Sidecar ist leichtgewichtig — nur `websockets`; Browser/Playwright laufen serverseitig in der Sandbox.)*

> **Linux ist hier bereits gebaut** (`sidecar-bin/jarvis-client-x86_64-unknown-linux-gnu` liegt vor, im
> `.deb` enthalten). Für **Windows** nur die `.exe`-Triple-Datei wie oben erzeugen, dann bauen.
> macOS analog (`…-apple-darwin`), derzeit nicht erforderlich.

## Windows: kein Konsolenfenster
Der Sidecar läuft unsichtbar im Hintergrund. Dafür sorgen zwei Dinge:
- die App startet den Sidecar mit `CREATE_NO_WINDOW` (in `main.rs`, nur Windows),
- die Sidecar-Binary wird mit `--noconsole` gebaut (`build-sidecar.ps1`).

Falls nach einem Update noch ein Konsolenfenster auftaucht: **App neu bauen** (`cargo tauri build`) —
das genügt bereits (CREATE_NO_WINDOW). Optional zusätzlich die Sidecar-Binary mit dem aktualisierten
`build-sidecar.ps1` neu erzeugen.

## Sicherheit
Die Durchsetzung liegt im Sidecar (`policy.json`: allow/ask/deny, Pfad-Scopes, Audit-Log) — siehe
`../client/README.md`. Diese App ist nur die komfortable Verwaltung dazu. Bei `ask`-Aktionen erscheint
der Bestätigungsdialog des Sidecars (zenity/osascript/MessageBox); ohne Bestätigungskanal → Ablehnung.

## Dateien
- `src-tauri/` — Rust (Tray, Sidecar-Supervisor, Commands), `tauri.conf.json`, `capabilities/`, `icons/`
- `ui/` — Frontend (statisch, kein Node-Bundler nötig; `index.html`/`main.js`/`style.css`)
