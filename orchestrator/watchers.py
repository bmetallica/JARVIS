"""Watcher-Automatisierungen: ein günstiges Python-Prüfskript läuft pro Tick in der
gehärteten Code-Sandbox und meldet nur, OB sich etwas geändert hat. Der (teure) LLM-Lauf
wird erst bei einem echten Treffer ausgelöst. Geteilt von app.py (Ausführung/Self-Heal)
und tools.py (Anlegen + Test vor dem Speichern)."""
import json

import sandbox

# Nach so vielen aufeinanderfolgenden Skript-Fehlern versucht der Agent eine Selbst-Reparatur.
FAIL_THRESHOLD = 3

# Marker, mit dem das Skript sein Ergebnis ausgibt (robust gegen sonstige Ausgaben).
_MARK = "___WATCH___"

# Vertrag, den das LLM beim Schreiben/Reparieren des Skripts einhalten muss.
SCRIPT_CONTRACT = (
    "Schreibe ein Python-PRÜFSKRIPT für die Sandbox (nur Standardbibliothek + `requests`).\n"
    "Vorgegeben sind: die Variable `state` (dict, beim ersten Lauf leer `{}`) und die Funktion\n"
    "`emit(triggered: bool, summary: str = '', state: dict = None)`.\n"
    "Ablauf: zu überwachende Quelle abrufen → mit `state` vergleichen → GENAU EINMAL `emit(...)` aufrufen:\n"
    "- `triggered=True`, wenn etwas Neues/Relevantes vorliegt; `summary` = kurzer Text, WAS neu ist "
    "(dieser Text wird dem Nutzer gemeldet bzw. dient dem LLM als Kontext).\n"
    "Merk-Zustand aktualisieren: ändere einfach das `state`-dict (z.B. `state['last_url'] = url`) — der\n"
    "aktuelle Inhalt von `state` wird beim `emit(...)` automatisch gemerkt (oder gib ihn explizit: "
    "`emit(True, summary, {'last_url': url})`).\n"
    "- `triggered=True` mit kurzem `summary`, wenn etwas Neues vorliegt; `triggered=False` sonst.\n"
    "Robust halten: HTTP-Timeout setzen, try/except, sinnvollen User-Agent-Header senden. "
    "KEINE Endlosschleifen, kein input(), keine interaktiven Eingaben. Das Skript läuft headless und einmalig."
)


def _prelude(state: dict) -> str:
    """Stellt dem Skript `state` und `emit(...)` bereit (Ergebnis wird mit Marker ausgegeben)."""
    sj = json.dumps(state or {}, ensure_ascii=False)
    return (
        "import json as _json\n"
        f"state = _json.loads({sj!r})\n"
        "def emit(triggered, summary='', state=None):\n"
        # Ohne expliziten state-Parameter wird der AKTUELLE (ggf. im Skript mutierte) `state` gemerkt.
        "    _s = state if state is not None else globals().get('state', {})\n"
        f"    print({_MARK!r} + _json.dumps({{'triggered': bool(triggered), 'summary': summary or '', 'state': _s}}))\n"
    )


def parse_output(stdout: str) -> dict | None:
    """Letzte gültige emit()-Ausgabe aus stdout holen."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith(_MARK):
            try:
                obj = json.loads(line[len(_MARK):])
                if isinstance(obj, dict) and "triggered" in obj:
                    return obj
            except Exception:
                return None
    return None


def run_check(a: dict, namespace: str) -> dict:
    """Prüfskript der Automation `a` einmal in der Sandbox ausführen (synchron).
    Rückgabe: {"ok": True, "parsed": {triggered, summary, state}} ODER {"ok": False, "error": str}."""
    script = a.get("check_script") or ""
    if not script.strip():
        return {"ok": False, "error": "Kein Prüfskript hinterlegt."}
    code = _prelude(a.get("state") or {}) + script
    res = sandbox.execute(code, "python", namespace, allow_network=bool(a.get("net", True)))
    if res.get("disabled"):
        return {"ok": False, "error": "Code-Sandbox ist deaktiviert (Admin)."}
    if res.get("offline"):
        return {"ok": False, "error": res.get("stderr", "Code-Sandbox nicht erreichbar.")}
    if not res.get("ok"):
        return {"ok": False, "error": (res.get("stderr") or "Skript-Fehler").strip()[:800]}
    parsed = parse_output(res.get("stdout"))
    if parsed is None:
        return {"ok": False, "error": "Skript rief emit(...) nicht gültig auf.\nstdout:\n"
                + (res.get("stdout") or "")[:500] + "\nstderr:\n" + (res.get("stderr") or "")[:300]}
    return {"ok": True, "parsed": parsed}


def strip_code_fences(text: str) -> str:
    """Markdown-Codeblock-Zäune entfernen, falls das LLM das Skript so verpackt."""
    t = (text or "").strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl != -1 else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()
