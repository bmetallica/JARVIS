"""Selbst-gebaute Skills: Jarvis schreibt wiederverwendbare, parametrisierte Werkzeuge,
die in der gehärteten Code-Sandbox laufen. **Global** (alle Nutzer teilen sie); ein Admin kann
sie im Admin-UI deaktivieren/editieren/löschen. Registry in skills.json; Ausführung analog
watchers.py (dem Code werden `args` + `result()` bereitgestellt)."""
import json
import re
import time
from pathlib import Path

import sandbox

_PATH = Path(__file__).with_name("skills.json")
_MARK = "___SKILL___"

# Vertrag, den das LLM beim Schreiben eines Skills einhält (auch im Tool-Schema referenziert).
SKILL_CONTRACT = (
    "Schreibe ein Python-Skill für die Sandbox (nur Standardbibliothek + `requests`).\n"
    "Definiere GENAU EINE Funktion `def run(args):`, die die Eingaben aus dem dict `args` liest und das "
    "Ergebnis per `return` zurückgibt (Zahl, Text, Liste oder dict — wird dem Aufrufer gemeldet).\n"
    "Die Werte in `args` können als String ankommen — wandle Zahlen IMMER sicher um (int()/float()).\n"
    "Beispiel: `def run(args):\\n    return float(args['a']) + float(args['b'])`\n"
    "Die Funktion wird automatisch mit den Aufruf-Argumenten ausgeführt — schreibe selbst KEINEN Aufruf.\n"
    "Bei `params` darfst du Typen angeben, z.B. {'a': {'type': 'number', 'description': '...'}}, sonst gilt Text.\n"
    "WICHTIG — ABHÄNGIGKEITEN: Nutzt dein Code etwas außerhalb der Standardbibliothek, MUSST du es angeben, "
    "damit es installiert wird: `pip` = Python-Pakete (z.B. ['paramiko','dnspython']), `apt` = System-Programme "
    "(z.B. ['nmap','arp-scan']). Lass dir nichts vorinstalliert sein — deklariere ALLES, was du importierst/aufrufst.\n"
    "Robust halten: HTTP-Timeout, try/except, sinnvoller User-Agent. KEINE Endlosschleifen, kein input()."
)

_items: dict[str, dict] = {}
_seq = 0


# ── Persistenz ───────────────────────────────────────────────────────────────
def _load() -> None:
    global _items, _seq
    try:
        d = json.loads(_PATH.read_text(encoding="utf-8"))
        _items = {s["id"]: s for s in d.get("items", [])}
        _seq = d.get("seq", 0)
        for s in _items.values():                        # Altbestand um neue Felder ergänzen
            s.setdefault("trust", "sandbox")
            s.setdefault("autonomous_ok", False)
            s.setdefault("net", False)
            s.setdefault("apt", [])
            s.setdefault("pip", [])
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[skills] Laden fehlgeschlagen: {e}")


def _save() -> None:
    try:
        _PATH.write_text(json.dumps({"seq": _seq, "items": list(_items.values())},
                                    indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[skills] Speichern fehlgeschlagen: {e}")


def sanitize_name(name: str) -> str:
    n = re.sub(r"[^a-z0-9_]", "_", (name or "").strip().lower()).strip("_")
    return n[:40] or "skill"


def _by_name(name: str) -> dict | None:
    sn = sanitize_name(name)
    for s in _items.values():
        if s["name"] == sn:
            return s
    return None


# ── Ausführung in der Sandbox ────────────────────────────────────────────────
def _wrap(code: str, args: dict) -> str:
    """Skill-Code einbetten: `args` bereitstellen, danach `run(args)` aufrufen und Ergebnis ausgeben."""
    tmpl = (
        "import site as _site, sys as _sys\n"                 # pip --user-Pakete importierbar machen (auch unter -I)
        "_sys.path.append(_site.getusersitepackages())\n"
        "import json as _json\n"
        "args = _json.loads(__ARGS__)\n"
        "__CODE__\n"
        "_run = globals().get('run')\n"
        "if not callable(_run):\n"
        "    _out = {'error': 'Das Skill muss eine Funktion run(args) definieren, die das Ergebnis per return zurueckgibt.'}\n"
        "else:\n"
        "    _out = {'result': _run(args)}\n"
        "print('__MARK__' + _json.dumps(_out, default=str))\n"
    )
    return (tmpl
            .replace("__ARGS__", repr(json.dumps(args or {}, ensure_ascii=False)))
            .replace("__MARK__", _MARK)
            .replace("__CODE__", code or ""))


def _parse(stdout: str):
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith(_MARK):
            try:
                return json.loads(line[len(_MARK):])
            except Exception:
                return None
    return None


def syntax_ok(code: str) -> tuple[bool, str]:
    """Leichter Check ohne Ausführung: Syntax gültig + Funktion run(args) vorhanden."""
    try:
        compile(code or "", "<skill>", "exec")
    except SyntaxError as e:
        return False, f"Syntaxfehler: {e}"
    if "def run" not in (code or ""):
        return False, "Der Code muss eine Funktion `def run(args):` definieren."
    return True, ""


def run_skill_code(code: str, args: dict, namespace: str, net: bool, trust: str = "sandbox") -> dict:
    """Skill-Code einmal ausführen. trust='elevated' → privilegierte Spur (Hostnetz+NET_RAW).
    Rückgabe {"ok": True, "result": …} oder {"ok": False, "error": …}."""
    priv = (trust == "elevated")
    res = sandbox.execute(_wrap(code, args), "python", namespace, allow_network=bool(net), privileged=priv)
    if res.get("disabled"):
        return {"ok": False, "error": "Code-Sandbox ist deaktiviert (Admin)."}
    if res.get("offline"):
        return {"ok": False, "error": res.get("stderr", "Code-Sandbox nicht erreichbar.")}
    if not res.get("ok"):
        return {"ok": False, "error": (res.get("stderr") or "Skript-Fehler").strip()[:800]}
    parsed = _parse(res.get("stdout"))
    if parsed is None:
        return {"ok": False, "error": "Skill gab kein Ergebnis aus.\n" + (res.get("stdout") or "")[:400]}
    if "error" in parsed:
        return {"ok": False, "error": parsed["error"]}
    return {"ok": True, "result": parsed.get("result")}


# ── CRUD ─────────────────────────────────────────────────────────────────────
def create(name: str, description: str, code: str, params: dict | None = None,
           owner_user_id: int | None = None, net: bool = False,
           apt: list | None = None, pip: list | None = None) -> dict:
    global _seq
    sn = sanitize_name(name)
    ex = _by_name(sn)                                    # gleicher Name → neue Version (ersetzen)
    if ex:
        # Neuer Code vom LLM → erhöhte Rechte zurücksetzen (Admin muss neu prüfen/freigeben).
        ex.update({"description": (description or "").strip(), "code": code or "",
                   "params": params or {}, "net": bool(net), "enabled": True,
                   "apt": list(apt or []), "pip": list(pip or []),
                   "version": ex.get("version", 1) + 1, "trust": "sandbox", "autonomous_ok": False})
        _save()
        return ex
    _seq += 1
    s = {
        "id": f"s{_seq}", "name": sn, "description": (description or "").strip(),
        "params": params or {}, "code": code or "", "owner_user_id": owner_user_id,
        "net": bool(net), "enabled": True, "version": 1,
        "trust": "sandbox",            # "sandbox" (isoliert) | "elevated" (Hostnetz+NET_RAW) — NUR Admin
        "autonomous_ok": False,        # erhöhte Skills dürfen nur autonom laufen, wenn Admin das extra erlaubt
        "apt": list(apt or []),        # benötigte System-Pakete (nur in erhöhter Spur installierbar)
        "pip": list(pip or []),        # benötigte Python-Pakete
        "run_count": 0, "fail_count": 0, "last_error": None, "created_at": time.time(),
    }
    _items[s["id"]] = s
    _save()
    return s


def update(name: str, **fields) -> dict | None:
    s = _by_name(name)
    if not s:
        return None
    for k in ("description", "code", "params", "net", "enabled", "trust", "autonomous_ok", "apt", "pip"):
        if k in fields and fields[k] is not None:
            s[k] = fields[k]
    s["version"] = s.get("version", 1) + 1
    _save()
    return s


def delete(name: str) -> bool:
    s = _by_name(name)
    if s:
        _items.pop(s["id"], None)
        _save()
    return bool(s)


def get(name: str) -> dict | None:
    return _by_name(name)


def list_all() -> list[dict]:
    return sorted(_items.values(), key=lambda s: s["name"])


def all_enabled() -> list[dict]:
    return [s for s in list_all() if s.get("enabled")]


def search(query: str) -> list[dict]:
    q = (query or "").strip().lower()
    items = all_enabled()
    return items if not q else [s for s in items if q in s["name"] or q in s["description"].lower()]


def record_run(name: str, ok: bool, error: str | None = None) -> None:
    s = _by_name(name)
    if not s:
        return
    s["run_count"] = s.get("run_count", 0) + 1
    if ok:
        s["fail_count"], s["last_error"] = 0, None
    else:
        s["fail_count"] = s.get("fail_count", 0) + 1
        s["last_error"] = (error or "")[:300]
    _save()


# ── Typisierte Tool-Schemas (deferred: on demand via load_skills) ─────────────
def _param_schema(params: dict) -> dict:
    """Lockere params ({name: "Beschreibung"} ODER {name: {type, description}}) → JSON-Schema."""
    props = {}
    for k, v in (params or {}).items():
        if isinstance(v, dict):
            props[k] = {"type": v.get("type", "string"), "description": str(v.get("description", ""))}
        else:
            props[k] = {"type": "string", "description": str(v)}
    return {"type": "object", "properties": props}


def tool_schema(s: dict) -> dict:
    return {"type": "function", "function": {
        "name": f"skill__{s['name']}",
        "description": (s.get("description") or s["name"]) + " (selbst-gebautes Skill)",
        "parameters": _param_schema(s.get("params")),
    }}


def schemas_for(names) -> list[dict]:
    """Tool-Schemas für die per load_skills geladenen Skills (für den Tool-Loop)."""
    out = []
    for n in names or []:
        s = _by_name(n)
        if s and s.get("enabled"):
            out.append(tool_schema(s))
    return out


# ── LLM-Anbindung (deferred: nur Namen+Beschreibung im Prompt) ────────────────
def catalog_hint() -> str:
    items = all_enabled()
    if not items:
        return ""
    lines = "\n".join(f"- {s['name']}: {s['description']}" for s in items[:60])
    return ("\n\nSELBST-GEBAUTE SKILLS (wiederverwendbare Werkzeuge): `run_skill(name, args)` ausführen "
            "(oder `load_skills([name])` → getipptes `skill__<name>`); `describe_skill(name)` für Details/Code; "
            "`create_skill(...)` neu; **`update_skill(name, code=…)` zum Ändern eines bestehenden Skills** (du darfst "
            "deine eigenen Skills jederzeit bearbeiten); `delete_skill(name)` löschen. Verfügbar:\n" + lines)


_load()
