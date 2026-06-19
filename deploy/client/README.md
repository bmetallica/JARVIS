# SH-Jarvis Client-Agent (Thin-Client / Python-Sidecar)

Verbindet einen Nutzer-Rechner mit dem Orchestrator und führt dort **lokale Aktionen** aus,
die JARVIS anfordert (Programme starten, Fenster, Medien/Lautstärke, Dateien, Zwischenablage,
Systeminfos). Das ist die Laufzeit von **Phase 2**; die Tauri-Tray-GUI wird später drumherum gebaut.

## Protokoll (WebSocket `/ws/client`)
- **Client→Server:** `hello{name,capabilities}` · `action_result{id,ok,result|error}` · `heartbeat`
- **Server→Client:** `welcome{session_id}` · `action{id,action,params}`

Der Server (Agent-Tool `client_action`) ruft eine Aktion auf und wartet via Request/Response-
Korrelation (Future je `id`) auf das Ergebnis. Der Agent bietet nur Aktionen an, die der Client
in seinen **Capabilities** meldet; zusätzlich greifen die normalen Tool-Rechte und die
Autonomie-Blacklist.

## Aktionen (38)
- **Programme/Skripte:** `app.launch` · `app.close` · `shell.run` · `open.url` · `open.path`
- **Fenster/Desktop/Eingabe:** `window.list|focus|close|minimize|maximize` · `screenshot` ·
  `input.type` · `input.hotkey` · `notify`
- **Medien/Lautstärke:** `media.play_pause|next|prev|stop` · `media.volume` · `volume.up|down|mute`
- **System:** `system.info` · `process.list` · `system.lock` · `system.suspend` · `system.shutdown` · `system.restart`
- **Dateien/Zwischenablage:** `fs.read|write|append|list|mkdir|move|copy|delete` · `clipboard.get|set`

`screenshot` wird vom Server-Tool **`client_screenshot`** genutzt (lokale Aufnahme → Vision-GPU = Pipeline).
`system.shutdown/restart` und `fs.delete` sind per Default **deny** (in `policy.json` freischaltbar).

**Plattform-Abdeckung:**
- **Windows:** voll über Bordmittel — Fenster/Eingabe via Win32 (`ctypes user32`), Medien/Lautstärke via
  virtuelle Tasten + Core-Audio (PowerShell), Notify als Tray-Balloon, Zwischenablage via `Get/Set-Clipboard`.
  Keine Zusatzpakete; Sidecar-Aufrufe von cmd/PowerShell laufen **ohne sichtbares Konsolenfenster**.
- **Linux:** Best-Effort über `wmctrl`/`xdotool`/`playerctl`/`pactl`/`grim`·`scrot`/`notify-send`/`xclip`.
- **macOS:** Best-Effort über `osascript`/`screencapture`/`pbcopy` (nicht vorrangig getestet).

Fehlt ein Tool oder läuft keine Desktop-Sitzung, meldet die Aktion sauber „nicht verfügbar".

## Start
```bash
pip install -r requirements.txt
JARVIS_SERVER=wss://192.168.66.224:8088 JARVIS_CLIENT_NAME="Arbeits-PC" python3 jarvis-client.py
```
Umgebungsvariablen:
- `JARVIS_SERVER` — Orchestrator (Standard `wss://192.168.66.224:8088`)
- `JARVIS_CLIENT_NAME` — Anzeigename/Gerät (Standard = Hostname)
- `JARVIS_CLIENT_ALLOW_SHELL` — `0` deaktiviert `shell.run` (wird dann nicht angeboten)
- `JARVIS_VERIFY_TLS` — `1` erzwingt Zertifikatsprüfung (Standard aus: selbstsigniert/LAN)

## Sicherheit — der Client ist die letzte Instanz
Der Client setzt eine **lokale Policy** durch, BEVOR er etwas ausführt — unabhängig davon, was
der Server schickt (Defense-in-Depth zusätzlich zu den Server-Rechten/Capability-Gating).

**`policy.json`** (wird beim ersten Start als Vorlage erzeugt). Je Aktion:
- `allow` — sofort ausführen
- `ask` — Bestätigung am Gerät nötig (Dialog via zenity/kdialog/osascript/MessageBox bzw.
  Terminal-Eingabe). **Ohne Bestätigungskanal → automatisch abgelehnt** (fail-safe).
- `deny` — gesperrt; solche Aktionen werden dem Server **gar nicht erst als Capability gemeldet**.

Zusätzlich:
- `fs_read_roots` / `fs_write_roots` — wenn gesetzt, sind `fs.read`/`fs.write` nur unterhalb dieser
  Verzeichnisse erlaubt (sonst `deny`).
- `deny`-Liste — Aktionen hart abschalten.
- `JARVIS_CLIENT_ALLOW_SHELL=0` — `shell.run` komplett deaktivieren.

**Audit:** Jede Anfrage + Entscheidung (allow/ask→erlaubt/ask→abgelehnt/deny) + Ergebnis wird mit
Zeitstempel in **`audit.log`** protokolliert.

> Defaults sind konservativ: lesende/anzeigende Aktionen `allow`, alles mit Wirkung
> (Programme/Shell starten, Dateien schreiben, Sperren) `ask`. Die grafische Berechtigungs-
> Verwaltung kommt mit der Tauri-GUI; bis dahin `policy.json` direkt bearbeiten.
Linux-Aktionen, die X11-Tools brauchen, funktionieren nur in einer Desktop-Sitzung.
