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


# Standard-Stichwörter (Smart-Home), genutzt wenn ein Server keine eigenen `accel_keywords` gesetzt hat.
# Treffer im Nutzertext → Geräte werden VORAB aufgelöst (Beschleunigung). Im Admin-UI je Server editierbar.
DEFAULT_ACCEL_KEYWORDS = [
    "licht", "lampe", "leuchte", "beleuchtung", "steckdose", "steckerleiste", "heizung", "thermostat",
    "rollladen", "rolladen", "jalousie", "rollo", "markise", "szene", "dimm", "smarthome", "smart home",
    "hausgerät", "garage", "ventilator", "strom", "watt", "verbrauch", "zähler", "energie", "kwh",
    "leistung", "photovoltaik", "solar", "sensor", "temperatur", "feuchtigkeit", "grad",
    "schalte", "schalt", "dimme", "öffne", "schließe",
]


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
        # Pro-Server-Stichwörter für die Vorauflösungs-Beschleunigung (NULL = Standardliste nutzen).
        c.execute("ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS accel_keywords TEXT;")
    _init = True


# ── DB-Verwaltung ──────────────────────────────────────────────────────────────

def list_servers() -> list[dict]:
    init()
    with store._conn() as c:
        rows = c.execute("SELECT id, name, url, enabled, accel_keywords FROM mcp_servers ORDER BY name;").fetchall()
    out = []
    for r in rows:
        info = _cache.get(r[1], {})
        out.append({"id": r[0], "name": r[1], "url": r[2], "enabled": r[3],
                    "tool_count": len(info.get("tools", [])), "error": info.get("error"),
                    "accel_keywords": r[4] or "", "accel_default": not bool(r[4]),
                    "has_device_search": bool(device_search_tool(r[1]))})
    return out


def set_keywords(name: str, keywords: str) -> None:
    """Stichwortliste (kommagetrennt) für einen Server setzen; leer → Standardliste."""
    init()
    kw = (keywords or "").strip() or None
    with store._conn() as c:
        c.execute("UPDATE mcp_servers SET accel_keywords=%s WHERE name=%s;", (kw, name))


def keywords_for(name: str) -> list[str]:
    """Effektive Stichwörter eines Servers: eigene (kommagetrennt) oder die Standardliste."""
    init()
    with store._conn() as c:
        r = c.execute("SELECT accel_keywords FROM mcp_servers WHERE name=%s;", (name,)).fetchone()
    raw = (r[0] if r else None) or ""
    if raw.strip():
        return [k.strip().lower() for k in raw.split(",") if k.strip()]
    return list(DEFAULT_ACCEL_KEYWORDS)


def device_search_tool(server: str) -> str | None:
    """Name des Geräte-Such-Tools eines Servers (für die Vorauflösung), per Heuristik."""
    info = _cache.get(server)
    if not info or not info.get("enabled"):
        return None
    for t in info.get("tools", []):
        n = t["name"].lower()
        if "search_device" in n or ("device" in n and "search" in n):
            return t["name"]
    return None


def match_accel(message: str):
    """Erster aktiver Server, dessen (eigene/Standard-)Stichwörter im Nutzertext vorkommen UND der
    ein Geräte-Such-Tool hat → (server, search_tool). Sonst None. Basis der Vorauflösungs-Beschleunigung."""
    msg = (message or "").lower()
    if not msg:
        return None
    for server, info in _cache.items():
        if not info.get("enabled"):
            continue
        search = device_search_tool(server)
        if not search:
            continue
        for kw in keywords_for(server):
            if kw and kw in msg:
                return server, search
    return None


def device_tool_names(server: str, limit: int = 8) -> list[str]:
    """Geräte-Steuer-Tools EINES Servers (Suchen/Schalten/Dimmen/…) zum Direkt-Vorladen."""
    info = _cache.get(server) or {}
    out = []
    for t in info.get("tools", []):
        hay = (t["name"] + " " + (t.get("description") or "")).lower()
        if _DEVICE_TOOL_RE.search(hay):
            out.append(f"mcp__{server}__{t['name']}")
    return out[:limit]


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
    return ("\n\nEXTERNE MCP-WERKZEUGE (u.a. SMART-HOME / Hausgeräte): Steuere Geräte wie Licht, Lampen, "
            "Steckdosen, Heizung/Thermostat, Rollläden/Jalousien und Szenen (an/aus, dimmen, schalten, "
            "öffnen/schließen) AUSSCHLIESSLICH über diese MCP-Werkzeuge — NIEMALS über client_action, browse "
            "oder ausgedachte URLs. Die Werkzeuge jedes Servers sind unten GELISTET — lade die gebrauchten "
            "DIREKT per `load_mcp_tools(['mcp__<server>__<tool>', …])` und rufe sie dann als "
            "`mcp__<server>__<tool>` auf. (`search_mcp_tools` nur zum Stöbern, und NUR mit englischem "
            "Funktions-Stichwort wie 'device'/'switch'/'dimmer' — NICHT mit deutschem Gerätenamen wie 'Lampe'.) "
            "Geräte-Ablauf: zuerst das GERÄTE-SUCH-Werkzeug (z.B. search_devices_tool) laden+aufrufen, um das "
            "Gerät und dessen `idx` zu finden, dann das Schalt-/Dimm-Werkzeug (z.B. set_switch_state, "
            "toggle_switch, set_dimmer_level) mit diesem `idx`. Suche das Gerät mit den ORIGINAL-WÖRTERN DES "
            "NUTZERS (Gerätename wie gesagt, z.B. 'Licht', 'Wohnzimmer') — NICHT ins Englische übersetzen. "
            "Findet die Suche nichts, mit einem kürzeren/anderen deutschen Wort erneut suchen, nicht aufgeben. "
            "Verfügbare Server/Tools:\n" + "\n".join(blocks))


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


_DEVICE_TOOL_RE = re.compile(r"(switch|dimmer|device|light|blind|scene|toggle|setpoint|color|"
                             r"temperature|licht|schalt)", re.IGNORECASE)


def find_tool(name_substr: str):
    """Erster aktiver Server + Tool, dessen Name den Teilstring enthält → (server, tool) oder None.
    Für serverseitige Vor-Auflösung (z.B. Geräte-Suche), ohne den Servernamen hart zu kodieren."""
    s = (name_substr or "").lower()
    for server, info in _cache.items():
        if not info.get("enabled"):
            continue
        for t in info.get("tools", []):
            if s in t["name"].lower():
                return server, t["name"]
    return None


def smarthome_tool_names(limit: int = 8) -> list[str]:
    """Voll-Namen der Geräte-Steuer-Tools aktiver Server (Suchen/Schalten/Dimmen/Szene/Rollladen).
    Wird bei Smart-Home-Befehlen DIREKT vorgeladen (ctx['loaded_mcp']), damit das Modell nicht erst
    den fehleranfälligen search_mcp_tools/load_mcp_tools-Umweg gehen muss."""
    out = []
    for server, info in _cache.items():
        if not info.get("enabled"):
            continue
        for t in info.get("tools", []):
            hay = (t["name"] + " " + (t.get("description") or "")).lower()
            if _DEVICE_TOOL_RE.search(hay):
                out.append(f"mcp__{server}__{t['name']}")
    return out[:limit]


def server_resources() -> list[str]:
    """Ressourcen-Namen fürs Rechtesystem (mcp:<server>)."""
    init()
    with store._conn() as c:
        rows = c.execute("SELECT name FROM mcp_servers ORDER BY name;").fetchall()
    return [f"mcp:{r[0]}" for r in rows]
