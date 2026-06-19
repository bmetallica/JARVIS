# JARVIS Code-Sandbox (Tier 3)

Isolierter Container, in dem JARVIS **selbstgeschriebenen Code** (Python/Shell) ausführt — getrennt
vom Host. Der Orchestrator führt nie selbst Code aus, sondern reicht ihn an diesen Dienst weiter.

## Start
```bash
cd deploy/sandbox
docker compose up -d --build
```
Der Dienst lauscht nur lokal: `http://127.0.0.1:8090` (der Orchestrator läuft auf demselben Host).

## Funktion
- `POST /exec` — Code ausführen (`language`: `python`|`shell`), gibt `stdout`/`stderr`/`exit_code` +
  neu erzeugte Dateien zurück.
- `GET /files`, `GET /file`, `POST /file`, `POST /reset` — Workspace-Dateien je Namespace.
- **browser_control** (headless Chromium/Playwright): `POST /browser/goto|act|content|screenshot|close` —
  persistente Browser-Sitzung je Namespace (Cookies/Logins bleiben erhalten).
- Pro Nutzer-Namespace ein eigenes, **persistentes** Workspace (`/workspace/<namespace>`, Docker-Volume).
- Vorinstalliert: requests, pandas, numpy, matplotlib, openpyxl, reportlab, pillow, beautifulsoup4, playwright.

> Basis-Image: `mcr.microsoft.com/playwright/python` (Chromium + Systemdeps vorinstalliert).
> Läuft als `pwuser` (uid 1001). Beim allerersten Start ein frisches Volume verwenden
> (Ownership = pwuser), sonst Schreibrechte-Konflikt mit Altbeständen.

## Sicherheit / Isolation
- Eigener Container, **Nicht-root**-Nutzer, **alle Capabilities gedroppt** (`cap_drop: ALL`).
- cgroup-Limits: `mem_limit 1g`, `pids_limit 256`, `cpus 2.0`; pro Job zusätzlich `setrlimit`
  (CPU-Zeit, max. Dateigröße, Prozesszahl) und ein **Timeout** mit Kill der Prozessgruppe.
- **Netzwerk pro Job abschaltbar:** Der Orchestrator-Toggle `sandbox_allow_network` (Admin-UI)
  steuert, ob ein Job Internet hat. Ohne Netz läuft der Job in einer eigenen, unprivilegierten
  Netz-Namespace (`unshare -rn`, nur loopback).
- `seccomp=unconfined` ist gesetzt, **damit** diese unprivilegierte Namespace-Isolation möglich ist
  (das Docker-Default-Profil verbietet `unshare(CLONE_NEWUSER)`). Die Schutzgrenze ist der separate,
  rechtlose Container + die cgroup-Limits. Für noch strengere Isolation bei nicht vertrauenswürdigem
  Code käme ein gVisor-/Kata-Runtime in Frage (Backlog).

## Orchestrator-Anbindung
- Client: `orchestrator/sandbox.py` · Tools: `run_python`, `run_shell`, `list_workspace_files`,
  `read_workspace_file` (in `tools.py`).
- Config (Admin-UI → System → Code-Sandbox): `sandbox_enabled`, `sandbox_allow_network`,
  `sandbox_url`, `sandbox_timeout_s`.
- Rechte: `tool:run_python` / `tool:run_shell` pro Gruppe; bei **autonomen** Läufen zusätzlich über
  die Autonomie-Blacklist sperrbar.
