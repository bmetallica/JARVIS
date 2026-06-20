"""
Messaging-Kanal (Telegram) — fester, geräteunabhängiger Kommunikationsweg.

Ausgehend: Automatisierungen/Timer/Ereignisse und der Agent können einem bestimmten Nutzer
zuverlässig schreiben (unabhängig davon, ob ein Browser-Tab offen ist).
Eingehend: Nutzer können JARVIS über Telegram anschreiben (Polling in app.py → Agenten-Loop).

Zuordnung Nutzer ↔ Telegram-Chat: Spalte `users.telegram_chat_id` (im Admin-UI pflegbar).
"""
from __future__ import annotations

import time

import requests

import auth
import config

# Unbekannte Absender (nicht zugeordnet) — nur protokolliert, NIE beschrieben/verarbeitet.
_pending: dict[str, dict] = {}


def _cfg() -> dict:
    return config.get()


def enabled() -> bool:
    c = _cfg()
    return bool(c.get("telegram_enabled")) and bool(c.get("telegram_bot_token"))


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{_cfg().get('telegram_bot_token','')}/{method}"


def is_verified(chat_id) -> bool:
    """SICHERHEIT: Eine Chat-ID gilt nur als verifiziert, wenn sie einem Nutzer zugeordnet
    ist ODER der explizit gesetzten Standard-Chat-ID entspricht. JARVIS schreibt/antwortet
    AUSSCHLIESSLICH an solche Kontakte."""
    cid = str(chat_id)
    if not cid:
        return False
    if cid == str(_cfg().get("telegram_default_chat_id", "") or ""):
        return True
    try:
        return auth.user_by_telegram_chat(cid) is not None
    except Exception:
        return False


def send_to_chat(chat_id: str, text: str) -> bool:
    """Einziger Sende-Chokepoint — verweigert JEDEN Versand an nicht verifizierte Chats."""
    if not enabled() or not chat_id or not text:
        return False
    if not is_verified(chat_id):
        print(f"[messaging] BLOCKIERT: Versand an nicht verifizierte Chat-ID {chat_id} abgelehnt.")
        return False
    try:
        r = requests.post(_api("sendMessage"), json={"chat_id": str(chat_id), "text": text}, timeout=10)
        return r.ok
    except Exception:
        return False


# ── Ausstehende (unverifizierte) Kontakte ────────────────────────────────────────
def add_pending(chat_id, name: str, text: str) -> None:
    _pending[str(chat_id)] = {"chat_id": str(chat_id), "name": name or "",
                              "text": (text or "")[:120], "ts": time.time()}
    if len(_pending) > 50:                       # einfache Obergrenze
        oldest = min(_pending, key=lambda k: _pending[k]["ts"])
        _pending.pop(oldest, None)


def pending() -> list[dict]:
    return sorted(_pending.values(), key=lambda p: p["ts"], reverse=True)


def clear_pending(chat_id=None) -> None:
    if chat_id is None:
        _pending.clear()
    else:
        _pending.pop(str(chat_id), None)


def chat_for_user(user_id) -> str:
    """Chat-ID eines Nutzers (per-Nutzer-Zuordnung), sonst der Standard-Chat (Fallback)."""
    cid = None
    try:
        cid = auth.telegram_chat_for_user(user_id)
    except Exception:
        pass
    return cid or _cfg().get("telegram_default_chat_id", "") or ""


def send_to_user(user_id, text: str) -> bool:
    return send_to_chat(chat_for_user(user_id), text)


def send_photo_to_chat(chat_id, img_bytes: bytes, caption: str = "", filename: str = "bild.png") -> bool:
    """Bild senden — gleicher Verifizierungs-Chokepoint wie Text (nur an verifizierte Chats)."""
    if not enabled() or not chat_id or not img_bytes:
        return False
    if not is_verified(chat_id):
        print(f"[messaging] BLOCKIERT: Foto an nicht verifizierte Chat-ID {chat_id} abgelehnt.")
        return False
    try:
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1000]
        r = requests.post(_api("sendPhoto"), data=data, files={"photo": (filename, img_bytes)}, timeout=30)
        return r.ok
    except Exception:
        return False


def send_photo_to_user(user_id, img_bytes: bytes, caption: str = "", filename: str = "bild.png") -> bool:
    return send_photo_to_chat(chat_for_user(user_id), img_bytes, caption, filename)


def user_for_chat(chat_id) -> dict | None:
    """Welcher JARVIS-Nutzer gehört zu diesem Telegram-Chat? (für eingehende Nachrichten)"""
    try:
        return auth.user_by_telegram_chat(str(chat_id))
    except Exception:
        return None


def get_updates(offset: int, timeout: int = 25) -> list[dict]:
    """Long-Poll auf eingehende Telegram-Nachrichten."""
    if not enabled():
        return []
    try:
        r = requests.get(_api("getUpdates"), params={"offset": offset, "timeout": timeout},
                         timeout=timeout + 10)
        if r.ok:
            return r.json().get("result", [])
    except Exception:
        pass
    return []


def bot_info() -> dict:
    if not enabled():
        return {"ok": False, "error": "deaktiviert oder kein Token"}
    try:
        r = requests.get(_api("getMe"), timeout=10)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
