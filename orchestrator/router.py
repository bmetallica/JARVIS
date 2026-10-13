"""
Embedding-Router — wählt pro Turn die eingeblendeten Tool-GRUPPEN (erweiterungen.md 11.3).

Warum: ~60 Kern-Tools dauerhaft im Prompt sind für kleine Modelle die Hauptquelle von
Fehlgriffen im No-Think-Pass UND blähen den Prefill. Der Router klassifiziert die Anfrage
mit dem bereits geladenen lokalen Embedding-Modell (fastembed, CPU, <10 ms) gegen deutsche
Beispielsätze pro Domäne und blendet nur die passenden Gruppen (+ Kernwerkzeuge) ein.

Sicherheitsnetze:
  • Score unter Schwelle / Fehler → VOLLE Tool-Liste (Verhalten wie bisher).
  • Tools, die in KEINER Gruppe stehen (z.B. künftig neue), sind IMMER sichtbar.
  • Der Denk-Retry des adaptive-Modus läuft grundsätzlich mit der VOLLEN Liste
    (ctx["all_tools"], siehe app.py) — ein Fehlrouting kostet also nur den schnellen Pass.
  • Abschaltbar: config.tool_router_enabled = false.
"""
from __future__ import annotations

import threading

import debug

# ── Domänen: Beispielsätze (werden einmalig embedded) ─────────────────────────
DOMAINS: dict[str, list[str]] = {
    "web": [
        "Such im Internet nach den neuesten Nachrichten.",
        "Hol mir die Schlagzeilen von tagesschau.de.",
        "Was kostet das iPhone aktuell? Recherchiere das bitte.",
        "Öffne die Webseite und lies mir den Artikel vor.",
        "Suche nach einem Rezept für Lasagne.",
        "Recherchiere die Hintergründe zu diesem Thema gründlich.",
    ],
    "calendar_todo": [
        "Was steht heute in meinem Kalender?",
        "Trag für morgen 15 Uhr Zahnarzt ein.",
        "Verschiebe den Termin am Freitag auf 17 Uhr.",
        "Schreib Milch kaufen auf meine To-do-Liste.",
        "Was ist auf meiner Aufgabenliste offen?",
        "Der Punkt Rasen mähen ist erledigt.",
    ],
    "client": [
        "Mach einen Screenshot von meinem Rechner.",
        "Öffne Firefox auf dem PC.",
        "Mach die Musik auf dem Computer leiser.",
        "Kopiere das in die Zwischenablage auf meinem Desktop.",
        "Starte das Programm auf der VM.",
    ],
    "dev": [
        "Baue mir ein Skill, das die Festplattenauslastung ausliest.",
        "Führe dieses Python-Skript aus und zeig mir das Ergebnis.",
        "Repariere das kaputte Skill.",
        "Schreibe ein Programm, das die Logdatei analysiert.",
        "Prüfe per Shell-Befehl, ob der Server erreichbar ist.",
        "Welche meiner Server brauchen Updates?",
    ],
    "automation": [
        "Erinnere mich jeden Morgen um 8 Uhr an die Medikamente.",
        "Prüfe stündlich, ob die Webseite erreichbar ist, und sag Bescheid.",
        "Welche Automatisierungen laufen gerade?",
        "Lösche die tägliche Erinnerung wieder.",
        "Wenn ein neues Gerät verbunden wird, benachrichtige mich.",
    ],
    "smalltalk": [
        "Wie geht es dir heute?",
        "Erzähl mir einen Witz.",
        "Was hältst du von künstlicher Intelligenz?",
        "Danke dir, das war hilfreich.",
        "Guten Morgen Jarvis.",
        "Wie spät ist es?",
    ],
}

# ── Tool-Gruppen (Domäne → sichtbare Tools) ───────────────────────────────────
GROUPS: dict[str, set[str]] = {
    "web": {"web_search", "fetch_url", "research", "browse", "browser_click", "browser_type",
            "browser_screenshot", "analyze_image", "send_image"},
    "calendar_todo": {"add_event", "list_events", "update_event", "delete_event", "share_calendar",
                      "unshare_calendar", "calendar_subscription", "subscribe_calendar",
                      "unsubscribe_calendar", "list_calendar_subscriptions", "add_todo", "list_todos",
                      "complete_todo", "remove_todo", "todo_link"},
    "client": {"list_clients", "list_client_capabilities", "client_screenshot", "client_action",
               "set_device_volume"},
    "dev": {"create_skill", "run_skill", "load_skills", "search_skills", "describe_skill",
            "update_skill", "delete_skill", "repair_skill", "run_python", "run_shell",
            "spawn_subagent", "list_workspace_files", "read_workspace_file"},
    "automation": {"create_automation", "create_watch_automation", "list_automations",
                   "update_automation", "cancel_automation"},
    "smalltalk": set(),                       # bewusst leer → nur die Kern-Tools
}

# Immer sichtbar: kleine, häufige, harmlose Tools + Meta-Zugänge zu allem Deferred.
# (search_/load_-Meta-Tools halten Skills & MCP IMMER erreichbar, egal wie geroutet wurde.)
ALWAYS: set[str] = {
    "set_timer", "list_timers", "cancel_timer", "weather",
    "save_memory", "save_note", "search_notes", "recall_conversation", "knowledge_search",
    "send_message", "create_user", "remember_my_voice", "link_voice_to_existing_user",
    "search_skills", "load_skills", "search_mcp_tools", "load_mcp_tools",
}

_lock = threading.Lock()
_domain_vecs: dict[str, list[list[float]]] | None = None


def _cos(a, b) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return s / (na * nb) if na and nb else 0.0


def _ensure_embeddings(cfg: dict) -> bool:
    """Beispielsätze einmalig embedden (lazy, thread-sicher)."""
    global _domain_vecs
    if _domain_vecs is not None:
        return True
    with _lock:
        if _domain_vecs is not None:
            return True
        try:
            import services
            vecs: dict[str, list] = {}
            for dom, examples in DOMAINS.items():
                vecs[dom] = services.embed(examples, cfg, task="search_document")
            _domain_vecs = vecs
            return True
        except Exception as e:
            debug.log("router_error", error=str(e)[:200])
            return False


def route(message: str, cfg: dict) -> tuple[set[str], float] | None:
    """Domänen der Anfrage per Embedding-Ähnlichkeit. None = unsicher → volle Tool-Liste."""
    if not message or not _ensure_embeddings(cfg):
        return None
    try:
        import services
        q = services.embed([message], cfg, task="search_query")[0]
    except Exception:
        return None
    scores = {dom: max(_cos(q, v) for v in vs) for dom, vs in _domain_vecs.items()}
    best_dom = max(scores, key=scores.get)
    best = scores[best_dom]
    threshold = float(cfg.get("tool_router_threshold", 0.45))
    if best < threshold:
        return None
    chosen = {d for d, s in scores.items() if s >= best - 0.05}
    return chosen, best


def filter_tools(schemas: list[dict], message: str, cfg: dict) -> tuple[list[dict], dict | None]:
    """Tool-Schemas auf die gerouteten Gruppen einschränken.
    Rückgabe (gefilterte_schemas, info|None). info=None → nicht geroutet (volle Liste)."""
    if not cfg.get("tool_router_enabled", True):
        return schemas, None
    r = route(message, cfg)
    if r is None:
        return schemas, None
    domains, score = r
    grouped = set().union(*GROUPS.values())
    allowed = set(ALWAYS)
    for d in domains:
        allowed |= GROUPS.get(d, set())
    out = [s for s in schemas
           if s.get("function", {}).get("name", "") not in grouped        # ungruppiert → immer sichtbar
           or s.get("function", {}).get("name", "") in allowed]
    info = {"domains": sorted(domains), "score": round(score, 3),
            "tools": len(out), "of": len(schemas)}
    debug.log("router", **info)
    return out, info
