"""
MCP-Hub — bindet externe MCP-Server (Streamable HTTP) als Werkzeuge ein.

  • Server werden im Admin-UI verwaltet (DB-Tabelle mcp_servers).
  • Tools jedes Servers werden gecacht und als OpenAI-Functions dem LLM angeboten,
    namespaced als  mcp__<server>__<tool>.
  • Autorisierung: jeder Server ist die Ressource  mcp:<server>  (im Rechtesystem),
    z.B. Smart-Home nur für bestimmte Gruppen.

Verbindung pro Aufruf (Streamable HTTP) — einfach und robust.
"""
from __future__ import annotations

import re

import store

_cache: dict = {}          # server_name -> {"url": str, "enabled": bool, "tools": [ {name,description,inputSchema} ], "error": str|None }
_init = False

_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def init() -> None:
    global _init
    if _init:
        return
    store.init()
    with store._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS mcp_servers (
            id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL,
            url TEXT NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT now());""")
    _init = True


# ── DB-Verwaltung ──────────────────────────────────────────────────────────────

def list_servers() -> list[dict]:
    init()
    with store._conn() as c:
        rows = c.execute("SELECT id, name, url, enabled FROM mcp_servers ORDER BY name;").fetchall()
    out = []
    for r in rows:
        info = _cache.get(r[1], {})
        out.append({"id": r[0], "name": r[1], "url": r[2], "enabled": r[3],
                    "tool_count": len(info.get("tools", [])), "error": info.get("error")})
    return out


def add_server(name: str, url: str) -> None:
    init()
    with store._conn() as c:
        c.execute("INSERT INTO mcp_servers (name, url) VALUES (%s, %s);", (name, url))


def remove_server(name: str) -> None:
    init()
    with store._conn() as c:
        c.execute("DELETE FROM mcp_servers WHERE name=%s;", (name,))
    _cache.pop(name, None)


def set_enabled(name: str, enabled: bool) -> None:
    init()
    with store._conn() as c:
        c.execute("UPDATE mcp_servers SET enabled=%s WHERE name=%s;", (enabled, name))
    if name in _cache:
        _cache[name]["enabled"] = enabled


# ── MCP-Verbindung ─────────────────────────────────────────────────────────────

async def _fetch_tools(url: str) -> list[dict]:
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.list_tools()
            return [{"name": t.name, "description": t.description or "",
                     "inputSchema": t.inputSchema or {"type": "object", "properties": {}}}
                    for t in res.tools]


async def call_tool(server: str, tool: str, args: dict) -> str:
    info = _cache.get(server)
    if not info:
        return f"MCP-Server „{server}“ ist nicht konfiguriert."
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession
    try:
        async with streamablehttp_client(info["url"]) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                result = await s.call_tool(tool, arguments=args or {})
    except Exception as e:
        return f"MCP-Aufruf an „{server}/{tool}“ fehlgeschlagen: {e}"
    parts = []
    for item in (result.content or []):
        txt = getattr(item, "text", None)
        parts.append(txt if txt is not None else str(item))
    out = "\n".join(parts).strip() or "(kein Ergebnis)"
    if getattr(result, "isError", False):
        return f"Fehler vom MCP-Tool: {out}"
    return out


# ── Cache / Refresh ────────────────────────────────────────────────────────────

async def refresh(name: str | None = None) -> None:
    """Tool-Listen (neu) laden. Ohne Name: alle Server."""
    init()
    with store._conn() as c:
        rows = c.execute("SELECT name, url, enabled FROM mcp_servers"
                         + (" WHERE name=%s" if name else "") + ";",
                         (name,) if name else ()).fetchall()
    for n, url, enabled in rows:
        entry = {"url": url, "enabled": enabled, "tools": [], "error": None}
        if enabled:
            try:
                entry["tools"] = await _fetch_tools(url)
            except Exception as e:
                entry["error"] = str(e)[:200]
        _cache[n] = entry


def tool_schemas() -> list[dict]:
    """Alle MCP-Tools aktiver Server als OpenAI-Function-Schemas (namespaced)."""
    out = []
    for server, info in _cache.items():
        if not info.get("enabled"):
            continue
        for t in info.get("tools", []):
            out.append({
                "type": "function",
                "function": {
                    "name": f"mcp__{server}__{t['name']}",
                    "description": f"[{server}] {t['description']}"[:1024],
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                },
            })
    return out


# ── Deferred Loading (#7): Katalog statt aller Schemas ───────────────────────
# Wie bei den Skills werden MCP-Tools NICHT mehr alle als Schema eingeblendet (das blähte den
# Prompt z.B. um 27 Domoticz-Tools und verschärfte die Tool-Verwechslung). Stattdessen: kompakter
# Katalog im System-Prompt + Meta-Tools search_mcp_tools/load_mcp_tools, die nur die gebrauchten
# Tools on demand sichtbar machen.

def has_servers() -> bool:
    """Gibt es überhaupt aktive MCP-Server mit Tools? (für die Denk-Heuristik)."""
    return any(info.get("enabled") and info.get("tools") for info in _cache.values())


def catalog_hint() -> str:
    """Kompakter Katalog aller aktiven MCP-Server + Tools (Name+Kurzzweck) für den System-Prompt."""
    blocks = []
    for server, info in _cache.items():
        if not info.get("enabled") or not info.get("tools"):
            continue
        names = ", ".join(t["name"] for t in info["tools"][:40])
        blocks.append(f"- {server}: {names}")
    if not blocks:
        return ""
    return ("\n\nEXTERNE MCP-WERKZEUGE (z.B. Smart-Home): NICHT direkt aufrufbar — erst mit "
            "`load_mcp_tools(['mcp__<server>__<tool>', …])` laden (oder `search_mcp_tools(query)` zum Finden), "
            "dann das geladene `mcp__<server>__<tool>` aufrufen. Verfügbare Server/Tools:\n" + "\n".join(blocks))


def search(query: str) -> list[dict]:
    """MCP-Tools nach Stichwort suchen → [{full_name, server, description}]."""
    q = (query or "").strip().lower()
    out = []
    for server, info in _cache.items():
        if not info.get("enabled"):
            continue
        for t in info.get("tools", []):
            full = f"mcp__{server}__{t['name']}"
            hay = (full + " " + (t.get("description") or "")).lower()
            if not q or q in hay:
                out.append({"full_name": full, "server": server,
                            "description": (t.get("description") or "")[:160]})
    return out


def _schema_for_full(full_name: str) -> dict | None:
    """OpenAI-Function-Schema für EIN MCP-Tool anhand seines vollen Namens mcp__<server>__<tool>."""
    if not full_name.startswith("mcp__") or full_name.count("__") < 2:
        return None
    _, server, tool = full_name.split("__", 2)
    info = _cache.get(server)
    if not info or not info.get("enabled"):
        return None
    for t in info.get("tools", []):
        if t["name"] == tool:
            return {
                "type": "function",
                "function": {
                    "name": full_name,
                    "description": f"[{server}] {t['description']}"[:1024],
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                },
            }
    return None


def schemas_for(names) -> list[dict]:
    """Schemas der per load_mcp_tools geladenen MCP-Tools (für den Tool-Loop)."""
    out = []
    for n in names or []:
        s = _schema_for_full(n)
        if s:
            out.append(s)
    return out


def server_resources() -> list[str]:
    """Ressourcen-Namen fürs Rechtesystem (mcp:<server>)."""
    init()
    with store._conn() as c:
        rows = c.execute("SELECT name FROM mcp_servers ORDER BY name;").fetchall()
    return [f"mcp:{r[0]}" for r in rows]
