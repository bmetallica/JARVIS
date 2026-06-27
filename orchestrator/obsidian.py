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


def _suggest_meta(cfg: dict, text: str, existing: list[str], own_title: str | None):
    """Ein LLM-Aufruf → (vorgeschlagener Titel | None, [Wikilinks]). Titel nur, wenn keiner vorgegeben ist."""
    import services
    ex = ", ".join(existing[:60])
    need_title = not own_title
    if need_title:
        sysmsg = ("Du organisierst eine Notiz für ein Obsidian-Wissensnetz. Gib EXAKT zwei Zeilen zurück:\n"
                  "Titel: <prägnant, 3–6 Wörter, kein Datum, keine Anführungszeichen>\n"
                  "Links: 2–4 passende Themen als Obsidian-Wikilinks [[…]] (bevorzugt VORHANDENE Titel)")
    else:
        sysmsg = ("Du verschlagwortest eine Notiz für ein Obsidian-Wissensnetz. Antworte AUSSCHLIESSLICH mit "
                  "2–4 passenden Themen als Obsidian-Wikilinks [[…]] (bevorzugt VORHANDENE Titel), z. B. [[Obst]] [[Ernährung]]")
    try:
        out = (services.llm_call(
            [{"role": "system", "content": sysmsg},
             {"role": "user", "content": f"Vorhandene Notizen/Themen: {ex or '(noch keine)'}\n\nNotiz: {text}"}],
            cfg, None, False).get("content", "") or "")
    except Exception:
        return None, []
    title = None
    if need_title:
        m = re.search(r"(?im)^\s*titel\s*:\s*(.+)$", out)
        if m:
            title = re.sub(r'["\[\]]', "", m.group(1)).strip()[:80] or None
    links, seen, clean = re.findall(r"\[\[([^\]\n]{1,60})\]\]", out), set(), []
    for l in links:
        l = re.sub(r"[\[\]#|^]", "", l).strip()
        cmp = own_title or title or ""
        if not l or (cmp and l.lower() == cmp.lower()) or l.lower() in seen:
            continue
        seen.add(l.lower()); clean.append(l)
    return title, clean[:max(0, int(cfg.get("obsidian_autolink_max", 4)))]


def _fallback_title(text: str) -> str:
    words = re.sub(r"\s+", " ", (text or "").strip()).split()
    return " ".join(words[:6])[:80] or "Notiz"


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
    mode = (cfg.get("obsidian_note_mode") or "file").lower()
    autolink = cfg.get("obsidian_autolink", True)

    # Ein LLM-Aufruf liefert (falls nötig) den Titel UND die Verlinkungen.
    gen_title, links = None, []
    if (autolink or (mode == "file" and not title)):
        gen_title, links = _suggest_meta(cfg, text, _list_note_titles(vault, inbox), title)
        if not autolink:
            links = []
    link_str = " ".join(f"[[{l}]]" for l in links)

    if mode == "inbox" and not title:                       # alter Sammel-Modus (optional)
        rel = inbox
        target = (vault / rel).resolve()
        suffix = f" {link_str}" if link_str else ""
        body = f"- [{time.strftime('%Y-%m-%d %H:%M')}] {text}{suffix}\n"
        header = "# Inbox\n\n"
    else:                                                   # jede Notiz = eigene Datei (eigener Graph-Knoten)
        eff_title = title or gen_title or _fallback_title(text)
        rel = _sanitize_title(eff_title) + ".md"
        target = (vault / rel).resolve()
        if not title and target.exists():                  # auto-Titel: keine fremde Notiz überschreiben/anhängen
            rel = _sanitize_title(eff_title) + " " + time.strftime("%H%M%S") + ".md"
            target = (vault / rel).resolve()
        body = (text if text.endswith("\n") else text + "\n")
        if link_str:
            body += f"\nVerwandt: {link_str}\n"
        header = ""

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
