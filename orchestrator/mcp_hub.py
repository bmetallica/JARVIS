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

import json
import re

import store

_cache: dict = {}          # server_name -> {"url", "enabled", "headers", "tools": [{name,description,inputSchema}], "error"}
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
        # Zugangsschlüssel als JSON-Header-Objekt, z.B. {"Authorization": "Bearer …"}
        # (phase1.md Baustein 4) — NULL = ohne Header verbinden, wie bisher.
        c.execute("ALTER TABLE mcp_servers ADD COLUMN IF NOT EXISTS headers TEXT;")
    _init = True


# ── Zugangsschlüssel (Header) ──────────────────────────────────────────────────

def parse_headers(raw: str) -> dict:
    """Eingabe aus dem Admin-UI → Header-Objekt. Zwei Schreibweisen sind erlaubt:
    ein JSON-Objekt ({"X-Key": "…"}) oder einfach der nackte Token → Bearer-Header.
    Leere Eingabe = keine Header."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except Exception as e:
            raise ValueError(f"Kein gültiges JSON: {e}")
        if not isinstance(obj, dict):
            raise ValueError("JSON muss ein Objekt sein, z.B. {\"Authorization\": \"Bearer …\"}.")
        out = {str(k): str(v) for k, v in obj.items() if str(k).strip()}
        if not out:
            raise ValueError("Das JSON-Objekt enthält keine Header.")
        return out
    return {"Authorization": f"Bearer {raw}"}


def _headers_hint(raw: str | None) -> str:
    """Maskierte Kurzfassung für die Admin-Liste: Header-Name + letzte 4 Zeichen."""
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
    except Exception:
        return "(unlesbar)"
    return ", ".join(f"{k}: …{str(v)[-4:]}" for k, v in (obj or {}).items())


def headers_for(name: str) -> dict:
    """Header dieses Servers (aus dem Cache, Fallback DB)."""
    info = _cache.get(name) or {}
    if "headers" in info:
        return info.get("headers") or {}
    init()
    with store._conn() as c:
        r = c.execute("SELECT headers FROM mcp_servers WHERE name=%s;", (name,)).fetchone()
    try:
        return json.loads(r[0]) if r and r[0] else {}
    except Exception:
        return {}


def set_headers(name: str, raw: str) -> None:
    """Zugangsschlüssel setzen (leer = entfernen). Wirft ValueError bei kaputtem JSON."""
    hdrs = parse_headers(raw)
    init()
    with store._conn() as c:
        c.execute("UPDATE mcp_servers SET headers=%s WHERE name=%s;",
                  (json.dumps(hdrs) if hdrs else None, name))
    if name in _cache:
        _cache[name]["headers"] = hdrs


# ── DB-Verwaltung ──────────────────────────────────────────────────────────────

def list_servers() -> list[dict]:
    init()
    with store._conn() as c:
        rows = c.execute("SELECT id, name, url, enabled, accel_keywords, headers "
                         "FROM mcp_servers ORDER BY name;").fetchall()
    out = []
    for r in rows:
        info = _cache.get(r[1], {})
        out.append({"id": r[0], "name": r[1], "url": r[2], "enabled": r[3],
                    "tool_count": len(info.get("tools", [])), "error": info.get("error"),
                    "accel_keywords": r[4] or "", "accel_default": not bool(r[4]),
                    "has_device_search": bool(device_search_tool(r[1])),
                    # Geheimnis NIE im Klartext ans UI — nur Namen der Header + maskierte Werte.
                    "headers_hint": _headers_hint(r[5]), "has_headers": bool(r[5])})
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


def matched_keywords(server: str, message: str) -> list[str]:
    """Stichwörter dieses Servers, die im Nutzertext vorkommen — längste zuerst.
    Die Listen werden aus den ECHTEN Gerätenamen geharvestet, taugen also als Suchwörter
    (anders als das gebeugte Nutzerwort: „Stromzählern" findet in Domoticz nichts)."""
    msg = (message or "").lower()
    if not msg:
        return []
    hits = [k for k in keywords_for(server) if k and len(k) >= 4 and k in msg]
    hits.sort(key=len, reverse=True)
    return hits


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


_HARVEST_STOP = {"und", "der", "die", "das", "mit", "von", "für", "auf", "aus", "bei", "neu", "alt",
                 "links", "rechts", "oben", "unten", "auto", "the", "off", "on"}


async def harvest_keywords(server: str) -> list[str]:
    """Beschleunigungs-Stichwörter automatisch aus den ECHTEN Gerätenamen des Servers gewinnen
    (erweiterungen.md 6.1) — beseitigt Lücken wie den „Bauwagen"-Fall dauerhaft.
    Sammelt Geräte über Breitensuche (viele Server liefern bei häufigen Buchstaben alles)."""
    tool = device_search_tool(server)
    if not tool:
        return []
    import json as _json
    names: set[str] = set()
    for q in ("", "e", "a", "i", "o", "u", "r", "n", "l", "t", "s"):
        try:
            raw = await call_tool(server, tool, {"query": q, "limit": 200})
            res = _json.loads(raw).get("result", []) or []
        except Exception:
            continue
        for d in res:
            if d.get("Name"):
                names.add(str(d["Name"]))
        if len(names) > 500:
            break
    words: set[str] = set()
    for n in names:
        for w in re.split(r"[^A-Za-zÄÖÜäöüß]+", n):
            w = w.strip().lower()
            if len(w) >= 3 and w not in _HARVEST_STOP:
                words.add(w)
    if not words:
        return []
    # Mit der Standardliste VEREINIGEN — generische Begriffe (temperatur/strom/schalte …) müssen
    # erhalten bleiben, auch wenn kein Gerät so heißt.
    kws = sorted(words | {k.lower() for k in DEFAULT_ACCEL_KEYWORDS})
    set_keywords(server, ", ".join(kws))
    return kws


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

def _client_factory(extra: dict):
    """httpx-Client-Factory, die die Zugangs-Header dieses Servers mitgibt.

    WICHTIG: In mcp >= 1.28 ist `streamablehttp_client(url, headers=…)` als „deprecated and
    will be ignored" markiert — die Header kommen dort NICHT mehr an (im Test nachgewiesen).
    Eigene Header gehen nur noch über diese Factory."""
    from mcp.shared._httpx_utils import create_mcp_http_client

    def factory(headers: dict | None = None, timeout=None, auth=None):
        merged = dict(headers or {})
        merged.update(extra or {})
        return create_mcp_http_client(merged, timeout, auth)

    return factory


def _connect(url: str, headers: dict | None = None):
    """Streamable-HTTP-Verbindung, mit Zugangs-Headern falls gesetzt."""
    from mcp.client.streamable_http import streamablehttp_client
    if headers:
        return streamablehttp_client(url, httpx_client_factory=_client_factory(headers))
    return streamablehttp_client(url)


async def _fetch_tools(url: str, headers: dict | None = None) -> list[dict]:
    from mcp import ClientSession
    async with _connect(url, headers) as (r, w, _):
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
    from mcp import ClientSession
    try:
        async with _connect(info["url"], info.get("headers")) as (r, w, _):
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
        rows = c.execute("SELECT name, url, enabled, headers FROM mcp_servers"
                         + (" WHERE name=%s" if name else "") + ";",
                         (name,) if name else ()).fetchall()
    for n, url, enabled, raw_hdrs in rows:
        try:
            hdrs = json.loads(raw_hdrs) if raw_hdrs else {}
        except Exception:
            hdrs = {}
        entry = {"url": url, "enabled": enabled, "headers": hdrs, "tools": [], "error": None}
        if enabled:
            try:
                entry["tools"] = await _fetch_tools(url, hdrs)
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
            "STATUS-Fragen ('Ist das Licht an?', 'Wie warm…?'): steht das Gerät mit aktuellem Wert bereits "
            "im Prompt (PASSENDE GERÄTE), antworte DIREKT daraus — kein Werkzeug nötig. "
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


def input_properties(server: str, tool: str) -> set[str]:
    """Parameternamen, die das inputSchema dieses MCP-Tools wirklich deklariert.
    Grundlage für den Schutz beim Einsetzen von Session-Anhängen (phase1.md Baustein 2):
    ersetzt wird nur, was das Tool auch kennt. Leere Menge = Schema unbekannt."""
    info = _cache.get(server) or {}
    for x in info.get("tools", []):
        if x["name"] == tool:
            props = ((x.get("inputSchema") or {}).get("properties") or {})
            return set(props.keys())
    return set()


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
