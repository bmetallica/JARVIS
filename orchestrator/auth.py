"""
Auth, Nutzer, Gruppen, Rechte — alles über das Admin-UI verwaltet (KEINE
hartkodierten Nutzer). Tabellen liegen in derselben Postgres-DB wie der
Vektor-Store (store.DSN).

Rechtemodell:
  • users            (Login)
  • groups           (Rollen; is_admin = Vollzugriff)
  • user_groups      (n:m)
  • group_permissions(group_id, resource)   resource = "tool:set_timer" | "mcp:smarthome" | "*"

Ein Nutzer darf eine Ressource, wenn eine seiner Gruppen sie (oder "*") erlaubt.
Initial wird admin/admin angelegt (must_change_password = true).
Voiceprints (Sprachbiometrie) referenzieren user_id — kommen im nächsten Schritt dazu.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time

import store

_PBKDF_ITERS = 200_000
_TOKEN_TTL = 8 * 3600

_sessions: dict = {}          # token -> {user_id, username, is_admin, exp}
_lock = threading.Lock()
_initialised = False


# ── Passwörter ────────────────────────────────────────────────────────────────

def _hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF_ITERS).hex()


def _make_hash(password: str) -> tuple[str, str]:
    salt = os.urandom(16)
    return _hash(password, salt), salt.hex()


def _verify(password: str, salt_hex: str, hash_hex: str) -> bool:
    return secrets.compare_digest(_hash(password, bytes.fromhex(salt_hex)), hash_hex)


# ── Schema + Seed ─────────────────────────────────────────────────────────────

def init() -> None:
    global _initialised
    with _lock:
        if _initialised:
            return
        store.init()
        with store._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS groups (
                id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                is_admin BOOLEAN NOT NULL DEFAULT FALSE);""")
            c.execute("""CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                password_hash TEXT, salt TEXT,
                must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT now());""")
            # passwortlose Nutzer erlauben (Passwort erst beim ersten Selbst-Login)
            c.execute("ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;")
            c.execute("ALTER TABLE users ALTER COLUMN salt DROP NOT NULL;")
            # Telegram-Chat-ID pro Nutzer (damit JARVIS gezielt schreiben/erkennen kann)
            c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_chat_id TEXT;")
            c.execute("""CREATE TABLE IF NOT EXISTS user_groups (
                user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                group_id BIGINT REFERENCES groups(id) ON DELETE CASCADE,
                PRIMARY KEY (user_id, group_id));""")
            c.execute("""CREATE TABLE IF NOT EXISTS group_permissions (
                group_id BIGINT REFERENCES groups(id) ON DELETE CASCADE,
                resource TEXT NOT NULL, PRIMARY KEY (group_id, resource));""")
            # Seed: Admin-Gruppe + admin/admin (nur wenn noch keine Nutzer existieren)
            n = c.execute("SELECT COUNT(*) FROM users;").fetchone()[0]
            if n == 0:
                gid = c.execute("INSERT INTO groups (name, is_admin) VALUES ('Administratoren', TRUE) "
                                "ON CONFLICT (name) DO UPDATE SET is_admin=TRUE RETURNING id;").fetchone()[0]
                c.execute("INSERT INTO group_permissions (group_id, resource) VALUES (%s, '*') "
                          "ON CONFLICT DO NOTHING;", (gid,))
                h, s = _make_hash("admin")
                uid = c.execute("INSERT INTO users (username, password_hash, salt, must_change_password) "
                                "VALUES ('admin', %s, %s, TRUE) RETURNING id;", (h, s)).fetchone()[0]
                c.execute("INSERT INTO user_groups (user_id, group_id) VALUES (%s, %s);", (uid, gid))
                print("[auth] Initialer Admin angelegt: admin/admin (Passwortwechsel erzwungen).")
        _initialised = True


# ── Login / Sessions ──────────────────────────────────────────────────────────

def needs_initial_password(username: str) -> bool:
    """True, wenn der Nutzer existiert, aber noch kein Passwort gesetzt hat."""
    init()
    with store._conn() as c:
        row = c.execute("SELECT password_hash FROM users WHERE username=%s;", (username,)).fetchone()
    return bool(row) and not row[0]


def set_initial_password(username: str, password: str) -> bool:
    """Setzt das Passwort beim ersten Selbst-Login — nur, wenn noch keins existiert."""
    init()
    if len(password) < 4:
        return False
    h, s = _make_hash(password)
    with store._conn() as c:
        cur = c.execute("UPDATE users SET password_hash=%s, salt=%s, must_change_password=FALSE "
                        "WHERE username=%s AND password_hash IS NULL;", (h, s, username))
        return cur.rowcount > 0


def login(username: str, password: str) -> dict | None:
    init()
    with store._conn() as c:
        row = c.execute("SELECT id, password_hash, salt, must_change_password FROM users WHERE username=%s;",
                        (username,)).fetchone()
    if not row or not row[1] or not _verify(password, row[2], row[1]):
        return None
    uid = row[0]
    token = secrets.token_urlsafe(32)
    sess = {"user_id": uid, "username": username, "is_admin": _is_admin(uid),
            "must_change": row[3], "exp": time.time() + _TOKEN_TTL}
    with _lock:
        _sessions[token] = sess
    return {"token": token, **sess}


def session(token: str | None) -> dict | None:
    if not token:
        return None
    with _lock:
        s = _sessions.get(token)
        if not s:
            return None
        if s["exp"] < time.time():
            _sessions.pop(token, None)
            return None
        return s


def logout(token: str) -> None:
    with _lock:
        _sessions.pop(token, None)


def change_password(user_id: int, new_password: str) -> None:
    h, s = _make_hash(new_password)
    with store._conn() as c:
        c.execute("UPDATE users SET password_hash=%s, salt=%s, must_change_password=FALSE WHERE id=%s;",
                  (h, s, user_id))
    # offene Sessions des Nutzers als 'geändert' markieren
    with _lock:
        for sess in _sessions.values():
            if sess["user_id"] == user_id:
                sess["must_change"] = False


# ── Nutzer / Gruppen / Rechte ──────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    with store._conn() as c:
        r = c.execute("SELECT 1 FROM user_groups ug JOIN groups g ON g.id=ug.group_id "
                      "WHERE ug.user_id=%s AND g.is_admin LIMIT 1;", (user_id,)).fetchone()
    return bool(r)


def list_users() -> list[dict]:
    with store._conn() as c:
        rows = c.execute("""SELECT u.id, u.username, u.must_change_password, (u.password_hash IS NOT NULL),
            COALESCE(array_agg(g.name) FILTER (WHERE g.name IS NOT NULL), '{}'), u.telegram_chat_id
            FROM users u LEFT JOIN user_groups ug ON ug.user_id=u.id
            LEFT JOIN groups g ON g.id=ug.group_id GROUP BY u.id ORDER BY u.username;""").fetchall()
    return [{"id": r[0], "username": r[1], "must_change": r[2], "has_password": r[3],
             "groups": list(r[4]), "telegram_chat_id": r[5] or ""}
            for r in rows]


def user_by_name(username: str) -> dict | None:
    with store._conn() as c:
        r = c.execute("SELECT id, username FROM users WHERE username=%s;", (username,)).fetchone()
    return {"id": r[0], "username": r[1]} if r else None


def username_by_id(user_id) -> str | None:
    if user_id is None:
        return None
    with store._conn() as c:
        r = c.execute("SELECT username FROM users WHERE id=%s;", (user_id,)).fetchone()
    return r[0] if r else None


def set_telegram_chat(user_id: int, chat_id: str) -> None:
    with store._conn() as c:
        c.execute("UPDATE users SET telegram_chat_id=%s WHERE id=%s;", (chat_id or None, user_id))


def telegram_chat_for_user(user_id) -> str | None:
    if user_id is None:
        return None
    with store._conn() as c:
        r = c.execute("SELECT telegram_chat_id FROM users WHERE id=%s;", (user_id,)).fetchone()
    return (r[0] if r else None) or None


def user_by_telegram_chat(chat_id: str) -> dict | None:
    with store._conn() as c:
        r = c.execute("SELECT id, username FROM users WHERE telegram_chat_id=%s;", (str(chat_id),)).fetchone()
    return {"id": r[0], "username": r[1]} if r else None


def create_user(username: str, password: str | None = None, group_ids: list[int] | None = None) -> int:
    """Legt einen Nutzer an. Ohne Passwort → passwortlos (Passwort beim ersten Selbst-Login)."""
    if password:
        h, s = _make_hash(password)
        mc = True
    else:
        h, s = None, None     # passwortlos
        mc = False
    with store._conn() as c:
        uid = c.execute("INSERT INTO users (username, password_hash, salt, must_change_password) "
                        "VALUES (%s,%s,%s,%s) RETURNING id;", (username, h, s, mc)).fetchone()[0]
        for gid in (group_ids or []):
            c.execute("INSERT INTO user_groups (user_id, group_id) VALUES (%s,%s) ON CONFLICT DO NOTHING;", (uid, gid))
    return uid


def is_admin(user_id: int) -> bool:
    return _is_admin(user_id)


def delete_user(user_id: int) -> None:
    with store._conn() as c:
        c.execute("DELETE FROM users WHERE id=%s;", (user_id,))


def set_user_groups(user_id: int, group_ids: list[int]) -> None:
    with store._conn() as c:
        c.execute("DELETE FROM user_groups WHERE user_id=%s;", (user_id,))
        for gid in group_ids:
            c.execute("INSERT INTO user_groups (user_id, group_id) VALUES (%s,%s) ON CONFLICT DO NOTHING;", (user_id, gid))


def admin_reset_password(user_id: int, new_password: str) -> None:
    h, s = _make_hash(new_password)
    with store._conn() as c:
        c.execute("UPDATE users SET password_hash=%s, salt=%s, must_change_password=TRUE WHERE id=%s;",
                  (h, s, user_id))


def list_groups() -> list[dict]:
    with store._conn() as c:
        rows = c.execute("""SELECT g.id, g.name, g.is_admin,
            COALESCE(array_agg(gp.resource) FILTER (WHERE gp.resource IS NOT NULL), '{}')
            FROM groups g LEFT JOIN group_permissions gp ON gp.group_id=g.id
            GROUP BY g.id ORDER BY g.name;""").fetchall()
    return [{"id": r[0], "name": r[1], "is_admin": r[2], "permissions": list(r[3])} for r in rows]


def create_group(name: str, is_admin: bool = False) -> int:
    with store._conn() as c:
        return c.execute("INSERT INTO groups (name, is_admin) VALUES (%s,%s) RETURNING id;",
                         (name, is_admin)).fetchone()[0]


def delete_group(group_id: int) -> None:
    with store._conn() as c:
        c.execute("DELETE FROM groups WHERE id=%s;", (group_id,))


def set_group_permissions(group_id: int, resources: list[str]) -> None:
    with store._conn() as c:
        c.execute("DELETE FROM group_permissions WHERE group_id=%s;", (group_id,))
        for res in resources:
            c.execute("INSERT INTO group_permissions (group_id, resource) VALUES (%s,%s) ON CONFLICT DO NOTHING;",
                      (group_id, res))


def allowed_resources(user_id: int) -> set[str]:
    """Vereinigung aller Gruppen-Rechte. '*' = alles."""
    with store._conn() as c:
        rows = c.execute("""SELECT DISTINCT gp.resource FROM user_groups ug
            JOIN group_permissions gp ON gp.group_id=ug.group_id WHERE ug.user_id=%s;""", (user_id,)).fetchall()
    return {r[0] for r in rows}


def is_allowed(user_id: int, resource: str) -> bool:
    res = allowed_resources(user_id)
    return "*" in res or resource in res


def managed_resources() -> set[str]:
    """Ressourcen, die in mindestens einer Gruppe unter Zugriffskontrolle stehen
    (ohne '*'). Alles andere ist offen für alle."""
    with store._conn() as c:
        rows = c.execute("SELECT DISTINCT resource FROM group_permissions WHERE resource <> '*';").fetchall()
    return {r[0] for r in rows}


def is_tool_allowed(user_id: int | None, resource: str) -> bool:
    """Eine Ressource ist OFFEN, solange sie keine Gruppe explizit listet.
    Sobald sie irgendwo gelistet ist, dürfen sie nur Admins oder Mitglieder
    einer gewährenden Gruppe. Gäste (user_id None) dürfen nur offene Ressourcen."""
    if resource not in managed_resources():
        return True
    if user_id is None:
        return False
    res = allowed_resources(user_id)
    return "*" in res or resource in res
