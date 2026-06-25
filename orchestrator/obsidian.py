"""
Obsidian-Notizen — schreibt Notizen direkt als Markdown in die Vault des jeweiligen Nutzers.

Obsidian (linuxserver-Container) bietet keine Schreib-API; Orchestrator und Obsidian teilen sich aber
den Host, sodass die Vault-Dateien direkt beschreibbar sind. Jeder Nutzer hat eine eigene Vault
(Zuordnung in `config.obsidian_vaults`, Schlüssel = JARVIS-Nutzername in Kleinschreibung).

Zwei Modi:
  • ohne Titel  → kurze Notiz wird mit Zeitstempel an die Inbox-Datei angehängt.
  • mit Titel   → eigene Note `<Titel>.md` (anlegen oder anhängen).

Sicherheit: Pfad-Containment (kein Ausbruch aus der Vault), Titel werden bereinigt. Neu erstellte
Dateien werden dem Vault-Besitzer übereignet (damit Obsidian sie weiter bearbeiten kann).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import config


def vault_for(username: str | None, cfg: dict | None = None) -> str | None:
    """Absoluter Vault-Pfad für einen Nutzer (per Nutzername, case-insensitive)."""
    if not username:
        return None
    cfg = cfg or config.get()
    vaults = cfg.get("obsidian_vaults") or {}
    return vaults.get(username.lower()) or vaults.get(username)


def _sanitize_title(title: str) -> str:
    """Dateinamen-sicherer Titel (keine Pfadtrenner, kein .., begrenzt)."""
    t = re.sub(r"[\\/:*?\"<>|\n\r\t]", " ", title or "").strip().strip(".")
    t = re.sub(r"\s+", " ", t)
    return t[:80] or "Notiz"


def _chown_to_vault(path: Path, vault: Path) -> None:
    """Neue Datei dem Besitzer des Vault-Ordners übereignen (Orchestrator läuft als root)."""
    try:
        st = vault.stat()
        os.chown(path, st.st_uid, st.st_gid)
    except Exception:
        pass


def save_note(username: str | None, text: str, title: str | None = None,
              cfg: dict | None = None) -> tuple[bool, str]:
    """Notiz in die Vault des Nutzers schreiben. Gibt (ok, Meldung) zurück."""
    cfg = cfg or config.get()
    if not cfg.get("obsidian_enabled", True):
        return False, "Die Obsidian-Anbindung ist deaktiviert."
    text = (text or "").strip()
    if not text:
        return False, "Die Notiz ist leer."
    vault_path = vault_for(username, cfg)
    if not vault_path:
        return False, (f"Für „{username or 'unbekannt'}“ ist keine Obsidian-Vault hinterlegt. "
                       "Der Admin kann sie unter config.obsidian_vaults (Nutzername → Vault-Pfad) eintragen.")
    vault = Path(vault_path).resolve()
    if not vault.is_dir():
        return False, f"Der hinterlegte Vault-Pfad existiert nicht: {vault_path}"

    if title:
        rel = _sanitize_title(title) + ".md"
        target = (vault / rel).resolve()
        body = text if text.endswith("\n") else text + "\n"
        header = ""
    else:
        rel = (cfg.get("obsidian_inbox") or "Inbox.md")
        target = (vault / rel).resolve()
        body = f"- [{time.strftime('%Y-%m-%d %H:%M')}] {text}\n"
        header = "# Inbox\n\n"

    # Pfad-Containment: Ziel muss innerhalb der Vault liegen.
    if not str(target).startswith(str(vault) + os.sep):
        return False, "Ungültiger Notiz-Pfad."

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        new_file = not target.exists()
        with open(target, "a", encoding="utf-8") as f:
            if new_file and header:
                f.write(header)
            f.write(body)
        if new_file:
            _chown_to_vault(target, vault)
    except Exception as e:
        return False, f"Konnte die Notiz nicht schreiben: {e}"

    where = target.name
    return True, f"Notiz in deiner Obsidian-Vault gespeichert ({where})."
