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


def _list_note_titles(vault: Path, inbox: str, limit: int = 80) -> list[str]:
    """Vorhandene Notiz-Titel (Dateinamen ohne .md) — als Kandidaten für konsistente Verlinkung."""
    titles = []
    try:
        for p in sorted(vault.glob("*.md")):
            if p.name == inbox or p.name.startswith("."):
                continue
            titles.append(p.stem)
            if len(titles) >= limit:
                break
    except Exception:
        pass
    return titles


def _suggest_links(cfg: dict, text: str, existing: list[str], own_title: str | None) -> list[str]:
    """Per LLM 2–4 passende Themen/Konzepte als Linkziele vorschlagen (bevorzugt vorhandene Titel)."""
    import services
    ex = ", ".join(existing[:60])
    messages = [
        {"role": "system", "content":
            "Du verschlagwortest eine Notiz für ein Obsidian-Wissensnetz. Nenne 2–4 prägnante Themen/Konzepte "
            "(Substantive), unter denen die Notiz sinnvoll einzuordnen ist, als Obsidian-Wikilinks. Bevorzuge "
            "VORHANDENE Titel, wenn sie thematisch passen; sonst sinnvolle Oberbegriffe. Antworte AUSSCHLIESSLICH "
            "mit den Links, z. B.: [[Obst]] [[Ernährung]]"},
        {"role": "user", "content": f"Vorhandene Notizen/Themen: {ex or '(noch keine)'}\n\nNotiz: {text}"},
    ]
    try:
        res = services.llm_call(messages, cfg, None, False)
        out = res.get("content", "") or ""
    except Exception:
        return []
    links = re.findall(r"\[\[([^\]\n]{1,60})\]\]", out)
    if not links:                          # Fallback: kommaseparierte Begriffe
        links = [t.strip() for t in re.split(r"[,\n]", out) if 1 < len(t.strip()) <= 40]
    seen, clean = set(), []
    for l in links:
        l = re.sub(r"[\[\]#|^]", "", l).strip()
        if not l or (own_title and l.lower() == own_title.lower()):
            continue
        if l.lower() not in seen:
            seen.add(l.lower()); clean.append(l)
    return clean[:max(0, int(cfg.get("obsidian_autolink_max", 4)))]


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

    inbox = (cfg.get("obsidian_inbox") or "Inbox.md")

    # Automatische Verknüpfung: passende Themen/Notizen als [[Wikilinks]] ermitteln.
    links = []
    if cfg.get("obsidian_autolink", True):
        existing = _list_note_titles(vault, inbox)
        links = _suggest_links(cfg, text, existing, title)
    link_str = " ".join(f"[[{l}]]" for l in links)

    if title:
        rel = _sanitize_title(title) + ".md"
        target = (vault / rel).resolve()
        body = (text if text.endswith("\n") else text + "\n")
        if link_str:
            body += f"\nVerwandt: {link_str}\n"
        header = ""
    else:
        rel = inbox
        target = (vault / rel).resolve()
        suffix = f" {link_str}" if link_str else ""
        body = f"- [{time.strftime('%Y-%m-%d %H:%M')}] {text}{suffix}\n"
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
    linked = f" Verknüpft mit {', '.join(links)}." if links else ""
    return True, f"Notiz in deiner Obsidian-Vault gespeichert ({where}).{linked}"
