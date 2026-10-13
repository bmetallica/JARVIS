"""
Weiterleitungsregeln (phase1.md Baustein 3) — deterministisch, ohne Agenten-Lauf.

Statt ein Präfix wie „rezept:" fest einzubauen, steht hier eine allgemeine Regel-Liste
(Config `forward_rules`, verwaltet im Admin-UI unter „📨 Weiterleitungen"):

    {"prefix": "rezept", "url": "http://host:8545/api/import", "token": "…",
     "types": ["text", "url", "image", "pdf"], "enabled": true}

Beginnt eine Telegram-Nachricht (oder eine Bildunterschrift) mit dem Präfix, geht sie samt
Datei per POST an die Adresse — und die Antwort des Ziels geht direkt zurück an Telegram.
Bei einem Fehler versucht JARVIS es NICHT zusätzlich selbst: sonst würde dieselbe Quelle
zweimal verarbeitet.

Wichtig: Der Aufrufer prüft VORHER, ob der Chat freigegeben ist. Dieses Modul kennt keine
Rechte — es leitet weiter, was man ihm gibt.
"""
from __future__ import annotations

import re

import debug

TIMEOUT_S = 10
_MAX_REPLY_CHARS = 800

# Erlaubte Quelltypen einer Regel; "url" ist ein Textfall (die Nachricht ist bloß ein Link).
KINDS = ("text", "url", "image", "pdf")

_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_PREFIX_RE_CACHE: dict = {}


def _prefix_re(prefix: str) -> re.Pattern:
    """„rezept" → trifft „rezept: …", „rezept …", „Rezept" (auch allein, für Foto+Caption)."""
    rx = _PREFIX_RE_CACHE.get(prefix)
    if rx is None:
        rx = re.compile(r"^\s*" + re.escape(prefix) + r"\s*(?::|\s|$)", re.IGNORECASE)
        _PREFIX_RE_CACHE[prefix] = rx
    return rx


def rules(cfg: dict) -> list[dict]:
    """Aktive, wohlgeformte Regeln — kaputte Einträge werden still übersprungen, damit ein
    Tippfehler im Admin-UI nicht den Telegram-Eingang lahmlegt."""
    out = []
    for r in (cfg.get("forward_rules") or []):
        if not isinstance(r, dict):
            continue
        prefix = str(r.get("prefix") or "").strip()
        url = str(r.get("url") or "").strip()
        if not prefix or not url.lower().startswith(("http://", "https://")):
            continue
        if not r.get("enabled", True):
            continue
        types = [str(x).strip().lower() for x in (r.get("types") or KINDS)]
        out.append({"prefix": prefix, "url": url, "token": str(r.get("token") or ""),
                    "types": [x for x in types if x in KINDS] or list(KINDS),
                    "enabled": True})
    return out


def kind_for_text(text: str) -> str:
    """Reiner Link → "url", sonst "text" (die Regel darf beides getrennt erlauben)."""
    return "url" if _URL_RE.match((text or "").strip()) else "text"


def match(cfg: dict, text: str, kind: str) -> tuple[dict | None, str]:
    """Passende Regel zu dieser Nachricht suchen.
    Gibt (Regel, Resttext ohne Präfix) zurück oder (None, "")."""
    raw = (text or "").strip()
    for rule in rules(cfg):
        m = _prefix_re(rule["prefix"]).match(raw)
        if not m:
            continue
        rest = raw[m.end():].strip().lstrip(":").strip()
        effective = kind_for_text(rest) if kind == "text" else kind
        if effective not in rule["types"]:
            continue
        return rule, rest
    return None, ""


def masked(cfg: dict) -> list[dict]:
    """Regeln fürs Admin-UI — der Zugangsschlüssel NIE im Klartext."""
    out = []
    for r in (cfg.get("forward_rules") or []):
        if not isinstance(r, dict):
            continue
        tok = str(r.get("token") or "")
        out.append({"prefix": r.get("prefix") or "", "url": r.get("url") or "",
                    "token_hint": ("…" + tok[-4:]) if tok else "", "has_token": bool(tok),
                    "types": r.get("types") or list(KINDS),
                    "enabled": bool(r.get("enabled", True))})
    return out


def _reply_from(resp) -> str:
    """Antwort des Ziels → eine Zeile für Telegram. Bevorzugt das JSON-Feld `message`
    (so wie der food-Service „📥 Rezept angenommen (Job #12)" schickt)."""
    try:
        data = resp.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        for key in ("message", "reply", "text", "detail", "status"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                msg = val.strip()
                job = data.get("job_id") or data.get("id")
                if job and str(job) not in msg:
                    msg += f" (Job #{job})"
                return msg[:_MAX_REPLY_CHARS]
        if data.get("job_id") or data.get("id"):
            return f"Angenommen (Job #{data.get('job_id') or data.get('id')})."
    body = (resp.text or "").strip()
    return body[:_MAX_REPLY_CHARS] if body else "Angenommen."


def forward(rule: dict, kind: str, text: str, *, file: tuple | None = None,
            requested_by: str = "", chat_id=None, source: str = "telegram") -> tuple[bool, str]:
    """Weiterleiten. `file` = (dateiname, bytes, mime) oder None.
    Gibt (ok, Nachricht für den Nutzer) zurück — blockierend, also im Thread aufrufen."""
    import requests

    data = {"source": source, "kind": kind}
    if text:
        data["text"] = text
    if requested_by:
        data["requested_by"] = requested_by
    if chat_id is not None:
        data["chat_id"] = str(chat_id)
    headers = {"Authorization": f"Bearer {rule['token']}"} if rule.get("token") else {}
    # IMMER multipart/form-data — auch ohne Datei. Sonst schickt requests urlencoded, und ein
    # Ziel, das nur multipart annimmt, sieht leere Felder.
    parts: list = [(k, (None, v)) for k, v in data.items()]
    if file:
        parts.append(("file", (file[0], file[1], file[2])))

    try:
        resp = requests.post(rule["url"], files=parts, headers=headers, timeout=TIMEOUT_S)
    except requests.Timeout:
        debug.log("forward_error", prefix=rule["prefix"], source_kind=kind, error="Timeout")
        return False, (f"Die Weiterleitung an „{rule['prefix']}“ hat zu lange gebraucht "
                       f"(über {TIMEOUT_S} s). Ich habe nichts weiter damit gemacht.")
    except Exception as e:
        debug.log("forward_error", prefix=rule["prefix"], source_kind=kind, error=str(e)[:200])
        return False, (f"Die Weiterleitung an „{rule['prefix']}“ ist fehlgeschlagen: "
                       f"{str(e)[:200]}. Ich habe nichts weiter damit gemacht.")

    ok = resp.status_code < 400
    msg = _reply_from(resp)
    debug.log("forward", prefix=rule["prefix"], source_kind=kind, url=rule["url"],
              status=resp.status_code, ok=ok, bytes=(len(file[1]) if file else 0),
              requested_by=requested_by, reply=msg[:200])
    if not ok:
        return False, (f"Das Ziel „{rule['prefix']}“ hat abgelehnt (HTTP {resp.status_code}): "
                       f"{msg}. Ich habe nichts weiter damit gemacht.")
    return True, msg
