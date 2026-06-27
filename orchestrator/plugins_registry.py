"""
Plugin-Registry — Verwaltung externer/interner Plugins, ihrer API-Keys, Scopes
und ihres KV-/Doc-Stores. Liegt in derselben Postgres-DB wie store.py/auth.py.

Tabellen (siehe pluginsystem.md, Kap. 10):
  • plugins          — registrierte Plugins (Manifest, enabled)
  • plugin_api_keys  — Bearer-Keys (nur Hash gespeichert), Scopes, optionale Nutzerbindung
  • plugin_kv        — schlüsselbasierter JSON-Store je Plugin/Namespace/Collection

Authentifizierung: Bearer-Token  jvp_<plugin>_<rand>  → verify_key() liefert den
Schlüssel-Kontext (plugin_id, scopes, user_binding). Autorisierung kombiniert
Key-Scopes (hier) UND Nutzerrechte (auth.is_tool_allowed) — UND-Verknüpfung.

Scopes (Strings, gleicher Namensraum wie auth.group_permissions.resource):
  api:llm api:vision api:stt api:tts api:rag api:storage api:notify
  api:events api:scheduler api:users api:act_as_user
  tool:<name>  mcp:<server>  plugin:<id>
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time

import auth
import store

_init = False
_lock = threading.Lock()

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,38}$")

# Bekannte API-Scopes (für UI/Validierung; tool:/mcp:/plugin: sind dynamisch).
API_SCOPES = [
    "api:llm", "api:vision", "api:stt", "api:tts", "api:rag", "api:storage",
    "api:notify", "api:events", "api:scheduler", "api:users", "api:act_as_user",
]

_PBKDF_ITERS = 120_000


def valid_id(pid: str) -> bool:
    return bool(_ID_RE.match(pid or ""))


# ── Schema ────────────────────────────────────────────────────────────────────

def init() -> None:
    global _init
    with _lock:
        if _init:
            return
        store.init()
        with store._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS plugins (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                version     TEXT,
                type        TEXT NOT NULL DEFAULT 'external',
                manifest    JSONB NOT NULL DEFAULT '{}'::jsonb,
                enabled     BOOLEAN NOT NULL DEFAULT TRUE,
                created_at  TIMESTAMPTZ DEFAULT now());""")
            c.execute("""CREATE TABLE IF NOT EXISTS plugin_api_keys (
                kid          TEXT PRIMARY KEY,
                plugin_id    TEXT REFERENCES plugins(id) ON DELETE CASCADE,
                label        TEXT,
                hash         TEXT NOT NULL,
                salt         TEXT NOT NULL,
                scopes       TEXT[] NOT NULL DEFAULT '{}',
                user_binding BIGINT,
                expires_at   TIMESTAMPTZ,
                revoked      BOOLEAN NOT NULL DEFAULT FALSE,
                last_used    TIMESTAMPTZ,
                created_at   TIMESTAMPTZ DEFAULT now());""")
            c.execute("""CREATE TABLE IF NOT EXISTS plugin_kv (
                plugin_id   TEXT NOT NULL,
                namespace   TEXT NOT NULL,
                collection  TEXT NOT NULL,
                key         TEXT NOT NULL,
                value       JSONB NOT NULL,
                updated_at  TIMESTAMPTZ DEFAULT now(),
                PRIMARY KEY (plugin_id, namespace, collection, key));""")
        _init = True


# ── Passwort-/Key-Hashing (analog auth.py) ────────────────────────────────────

def _hash(secret: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, _PBKDF_ITERS).hex()


def _make_hash(secret: str) -> tuple[str, str]:
    salt = os.urandom(16)
    return _hash(secret, salt), salt.hex()


def _verify(secret: str, salt_hex: str, hash_hex: str) -> bool:
    return secrets.compare_digest(_hash(secret, bytes.fromhex(salt_hex)), hash_hex)


# ── Plugin-CRUD ───────────────────────────────────────────────────────────────

def register(manifest: dict) -> dict:
    """Plugin registrieren/aktualisieren (upsert per id). manifest = plugin.json."""
    init()
    pid = (manifest.get("id") or "").strip().lower()
    if not valid_id(pid):
        raise ValueError("Ungültige Plugin-id (a-z, 0-9, -, _; 2–39 Zeichen, Beginn Buchstabe).")
    name = manifest.get("name") or pid
    version = str(manifest.get("version") or "0.0.0")
    ptype = manifest.get("type") or "external"
    with store._conn() as c:
        c.execute("""INSERT INTO plugins (id, name, version, type, manifest)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, version=EXCLUDED.version,
                type=EXCLUDED.type, manifest=EXCLUDED.manifest;""",
                  (pid, name, version, ptype, json.dumps(manifest)))
    return get(pid)


def get(pid: str) -> dict | None:
    init()
    with store._conn() as c:
        r = c.execute("SELECT id, name, version, type, manifest, enabled FROM plugins WHERE id=%s;",
                      (pid,)).fetchone()
    if not r:
        return None
    return {"id": r[0], "name": r[1], "version": r[2], "type": r[3],
            "manifest": r[4], "enabled": r[5]}


def list_plugins() -> list[dict]:
    init()
    with store._conn() as c:
        rows = c.execute("""SELECT p.id, p.name, p.version, p.type, p.enabled,
            COALESCE((SELECT COUNT(*) FROM plugin_api_keys k WHERE k.plugin_id=p.id AND NOT k.revoked),0)
            FROM plugins p ORDER BY p.id;""").fetchall()
    return [{"id": r[0], "name": r[1], "version": r[2], "type": r[3], "enabled": r[4],
             "active_keys": r[5]} for r in rows]


def set_enabled(pid: str, enabled: bool) -> None:
    init()
    with store._conn() as c:
        c.execute("UPDATE plugins SET enabled=%s WHERE id=%s;", (enabled, pid))


def delete(pid: str) -> None:
    init()
    with store._conn() as c:
        c.execute("DELETE FROM plugins WHERE id=%s;", (pid,))      # CASCADE löscht Keys
        c.execute("DELETE FROM plugin_kv WHERE plugin_id=%s;", (pid,))


# ── API-Keys ──────────────────────────────────────────────────────────────────

def create_key(pid: str, scopes: list[str], *, label: str = "",
               user_binding: int | None = None, ttl_days: int | None = None) -> str:
    """Neuen API-Key erzeugen. Gibt den Klartext-Token EINMALIG zurück (nur Hash wird gespeichert)."""
    init()
    if not get(pid):
        raise ValueError(f"Plugin „{pid}“ ist nicht registriert.")
    rand = secrets.token_urlsafe(24)
    token = f"jvp_{pid}_{rand}"
    kid = "k_" + secrets.token_hex(8)
    h, s = _make_hash(token)
    expires = None
    if ttl_days:
        with store._conn() as c:
            c.execute("""INSERT INTO plugin_api_keys (kid, plugin_id, label, hash, salt, scopes,
                user_binding, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s, now() + (%s||' days')::interval);""",
                      (kid, pid, label, h, s, list(scopes or []), user_binding, str(int(ttl_days))))
    else:
        with store._conn() as c:
            c.execute("""INSERT INTO plugin_api_keys (kid, plugin_id, label, hash, salt, scopes, user_binding)
                VALUES (%s,%s,%s,%s,%s,%s,%s);""",
                      (kid, pid, label, h, s, list(scopes or []), user_binding))
    return token


def list_keys(pid: str) -> list[dict]:
    init()
    with store._conn() as c:
        rows = c.execute("""SELECT kid, label, scopes, user_binding, expires_at, revoked, last_used, created_at
            FROM plugin_api_keys WHERE plugin_id=%s ORDER BY created_at;""", (pid,)).fetchall()
    return [{"kid": r[0], "label": r[1], "scopes": list(r[2] or []), "user_binding": r[3],
             "expires_at": str(r[4]) if r[4] else None, "revoked": r[5],
             "last_used": str(r[6]) if r[6] else None, "created_at": str(r[7])} for r in rows]


def revoke_key(kid: str) -> None:
    init()
    with store._conn() as c:
        c.execute("UPDATE plugin_api_keys SET revoked=TRUE WHERE kid=%s;", (kid,))


def set_key_scopes(kid: str, scopes: list[str]) -> None:
    init()
    with store._conn() as c:
        c.execute("UPDATE plugin_api_keys SET scopes=%s WHERE kid=%s;", (list(scopes or []), kid))


def verify_key(token: str | None) -> dict | None:
    """Bearer-Token prüfen. Gibt {kid, plugin_id, scopes, user_binding} oder None.
    Schnellfilter über das eingebettete Plugin-Präfix, dann Hash-Vergleich."""
    if not token or not token.startswith("jvp_"):
        return None
    init()
    # jvp_<plugin>_<rand> → Plugin-Präfix herauslösen (Plugin-id kann '_' enthalten,
    # daher gegen die Kandidaten der DB matchen statt blind zu splitten).
    with store._conn() as c:
        rows = c.execute("""SELECT k.kid, k.plugin_id, k.hash, k.salt, k.scopes, k.user_binding,
            k.revoked, k.expires_at, p.enabled
            FROM plugin_api_keys k JOIN plugins p ON p.id=k.plugin_id
            WHERE NOT k.revoked AND p.enabled
              AND %s LIKE 'jvp_'||k.plugin_id||'_%%';""", (token,)).fetchall()
    now = time.time()
    for kid, pid, h, salt, scopes, ub, revoked, exp, enabled in rows:
        if exp is not None and exp.timestamp() < now:
            continue
        if _verify(token, salt, h):
            try:
                with store._conn() as c:
                    c.execute("UPDATE plugin_api_keys SET last_used=now() WHERE kid=%s;", (kid,))
            except Exception:
                pass
            return {"kid": kid, "plugin_id": pid, "scopes": set(scopes or []), "user_binding": ub}
    return None


def has_scope(keyinfo: dict, scope: str) -> bool:
    sc = keyinfo.get("scopes") or set()
    return "*" in sc or scope in sc


# ── KV-/Doc-Store (api:storage) ───────────────────────────────────────────────

def kv_ns(plugin_id: str, user_id, scope: str = "user") -> str:
    """Namespace bestimmen: 'user' → pro Nutzer getrennt, 'shared' → Familien-Pool."""
    if scope == "shared":
        return f"plugin:{plugin_id}:shared"
    return f"plugin:{plugin_id}:u{user_id if user_id is not None else 'guest'}"


def kv_get(plugin_id: str, namespace: str, collection: str, key: str):
    init()
    with store._conn() as c:
        r = c.execute("""SELECT value FROM plugin_kv
            WHERE plugin_id=%s AND namespace=%s AND collection=%s AND key=%s;""",
                      (plugin_id, namespace, collection, key)).fetchone()
    return r[0] if r else None


def kv_set(plugin_id: str, namespace: str, collection: str, key: str, value) -> None:
    init()
    with store._conn() as c:
        c.execute("""INSERT INTO plugin_kv (plugin_id, namespace, collection, key, value, updated_at)
            VALUES (%s,%s,%s,%s,%s, now())
            ON CONFLICT (plugin_id, namespace, collection, key)
            DO UPDATE SET value=EXCLUDED.value, updated_at=now();""",
                  (plugin_id, namespace, collection, key, json.dumps(value)))


def kv_patch(plugin_id: str, namespace: str, collection: str, key: str, patch: dict) -> dict:
    """Teil-Merge bzw. atomares Inkrement: {"$inc":{"xp":50}} oder flacher Merge.
    Gibt den neuen Wert zurück."""
    init()
    with store._conn() as c:
        with c.cursor() as cur:
            cur.execute("""SELECT value FROM plugin_kv
                WHERE plugin_id=%s AND namespace=%s AND collection=%s AND key=%s FOR UPDATE;""",
                        (plugin_id, namespace, collection, key))
            row = cur.fetchone()
            cur_val = row[0] if row else {}
            if not isinstance(cur_val, dict):
                cur_val = {"_value": cur_val}
            new_val = dict(cur_val)
            inc = patch.get("$inc")
            if isinstance(inc, dict):
                for k, delta in inc.items():
                    new_val[k] = (new_val.get(k) or 0) + delta
            for k, v in patch.items():
                if k != "$inc":
                    new_val[k] = v
            cur.execute("""INSERT INTO plugin_kv (plugin_id, namespace, collection, key, value, updated_at)
                VALUES (%s,%s,%s,%s,%s, now())
                ON CONFLICT (plugin_id, namespace, collection, key)
                DO UPDATE SET value=EXCLUDED.value, updated_at=now();""",
                        (plugin_id, namespace, collection, key, json.dumps(new_val)))
    return new_val


def kv_delete(plugin_id: str, namespace: str, collection: str, key: str) -> int:
    init()
    with store._conn() as c:
        cur = c.execute("""DELETE FROM plugin_kv
            WHERE plugin_id=%s AND namespace=%s AND collection=%s AND key=%s;""",
                        (plugin_id, namespace, collection, key))
        return cur.rowcount


def kv_list(plugin_id: str, namespace: str, collection: str,
            prefix: str = "", limit: int = 100) -> list[dict]:
    init()
    with store._conn() as c:
        rows = c.execute("""SELECT key, value, updated_at FROM plugin_kv
            WHERE plugin_id=%s AND namespace=%s AND collection=%s AND key LIKE %s
            ORDER BY key LIMIT %s;""",
                         (plugin_id, namespace, collection, (prefix or "") + "%", limit)).fetchall()
    return [{"key": r[0], "value": r[1], "updated_at": str(r[2])} for r in rows]
