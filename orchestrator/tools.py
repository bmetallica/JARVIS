"""
Tool-Layer des Orchestrators.

TOOL_SCHEMAS  — OpenAI-Function-Definitionen, die dem LLM mitgegeben werden.
execute_tool  — führt ein Tool aus; bekommt einen Kontext (u.a. session_id),
                damit Ausgaben/Alarme an die Ursprungsquelle gebunden bleiben.

Erweiterbar: neue interne Tools (web_search, RAG, Wetter …) hier ergänzen.
"""
from __future__ import annotations

import asyncio
import time

import requests

import auth
import automations
import biometrics
import config
import debug
import knowledge
import mcp_hub
import messaging
import sandbox
import services
import timers
from session_hub import hub


def _http_json(url: str, params: dict, timeout: int = 10, retries: int = 3) -> dict:
    """GET mit JSON-Antwort und Retry — fängt transiente Verbindungsabbrüche ab
    (open-meteo & Co. trennen hier sporadisch jede zweite Verbindung)."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:       # noqa: BLE001
            last = e
            time.sleep(0.4 * (attempt + 1))
    raise last if last else RuntimeError("HTTP fehlgeschlagen")

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": "Setzt einen Timer/Wecker/eine Erinnerung für eine bestimmte Dauer. "
                           "Mehrere Timer gleichzeitig sind möglich. Der Alarm ertönt dort, wo der Timer erstellt wurde.",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_seconds": {"type": "integer", "description": "Dauer in Sekunden (z.B. 5 Min = 300)"},
                    "label": {"type": "string", "description": "Kurze Bezeichnung, z.B. 'Pizza' oder 'Tee'"},
                },
                "required": ["duration_seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_timers",
            "description": "Listet alle aktuell laufenden Timer dieser Session mit Restzeit auf.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_timer",
            "description": "Bricht einen laufenden Timer ab — per Bezeichnung (label) oder ID.",
            "parameters": {
                "type": "object",
                "properties": {"identifier": {"type": "string", "description": "Label oder ID des Timers"}},
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_clients",
            "description": "Listet die aktuell verbundenen Client-Rechner (PCs) mit Namen. Nutze dies, um zu "
                           "wissen, welche Geräte verfügbar sind und welchen Namen du als `device` bei "
                           "client_action angeben kannst.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_client_capabilities",
            "description": "Listet, welche Aktionen der verbundene Client-Rechner (PC) ausführen kann. "
                           "Vor client_action nutzen, wenn unklar ist, was möglich ist.",
            "parameters": {
                "type": "object",
                "properties": {"device": {"type": "string", "description": "Optional: Gerätename, sonst der aktive Client."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "client_screenshot",
            "description": "Macht einen Screenshot vom Bildschirm des Nutzer-PCs und analysiert ihn multimodal "
                           "(lokale Aufnahme → Vision). Nutze dies, um zu sehen/verstehen, was gerade auf dem "
                           "Bildschirm des Nutzers ist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Was möchtest du über den Bildschirm wissen? (optional)"},
                    "device": {"type": "string", "description": "Optional: Zielgerät, sonst der aktive Client."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "client_action",
            "description": "Führt eine Aktion auf dem verbundenen Client-RECHNER (PC) des Nutzers aus — z.B. ein "
                           "Programm starten, Fenster steuern, Lautstärke/Medien, Dateien/Zwischenablage. Nur für "
                           "Dinge, die lokal auf dem Nutzer-PC passieren sollen (nicht serverseitig).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": [
                        "app.launch", "app.close", "shell.run", "open.url", "open.path",
                        "window.list", "window.focus", "window.close", "window.minimize", "window.maximize",
                        "input.type", "input.hotkey", "notify",
                        "media.play_pause", "media.next", "media.prev", "media.stop",
                        "media.volume", "volume.up", "volume.down", "volume.mute",
                        "system.info", "process.list", "system.lock", "system.suspend",
                        "system.shutdown", "system.restart",
                        "fs.read", "fs.write", "fs.append", "fs.list", "fs.mkdir", "fs.move", "fs.copy", "fs.delete",
                        "clipboard.get", "clipboard.set"],
                        "description": "Auszuführende Aktion auf dem Client-PC."},
                    "params": {"type": "object", "description": "Parameter je Aktion, z.B. {\"name\":\"firefox\"}, "
                               "{\"command\":\"…\"}, {\"level\":40} (media.volume), {\"title\":\"…\"} (window.*), "
                               "{\"text\":\"…\"} (input.type/notify), {\"keys\":\"ctrl+s\"} (input.hotkey), "
                               "{\"path\":\"…\"} (fs/open), {\"src\":\"…\",\"dest\":\"…\"} (fs.move/copy), "
                               "{\"url\":\"…\"} (open.url)."},
                    "device": {"type": "string", "description": "Optional: Zielgerät (Name), sonst der aktive Client."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_device_volume",
            "description": "Stellt die Lautstärke des Geräts ein, mit dem gerade gesprochen wird (Satellit/Lautsprecher). "
                           "Stufe 1–10. Beispiel: 'Jarvis, Lautstärke 7'.",
            "parameters": {
                "type": "object",
                "properties": {"level": {"type": "integer", "description": "Lautstärke 1 (leise) bis 10 (laut)"}},
                "required": ["level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_datetime",
            "description": "Gibt das aktuelle Datum und die Uhrzeit zurück. Nutze dies bei Fragen "
                           "nach Zeit/Datum/Wochentag — rate niemals.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Aktuelles Wetter für einen Ort (Stadt/Region). Liefert Temperatur, "
                           "gefühlte Temperatur, Luftfeuchte, Wind und Wetterlage.",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string", "description": "Ort, z.B. 'Berlin' oder 'München'"}},
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Durchsucht das Web (aktuelle Infos, Fakten, News). Gib eine knappe Suchanfrage an. "
                           "Fasse die Treffer danach für den Nutzer auf Deutsch zusammen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Suchanfrage"},
                    "max_results": {"type": "integer", "description": "Anzahl Treffer (Standard 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Speichert dauerhaft einen persönlichen Fakt über den Nutzer (Name, Wohnort, "
                           "Beruf, Vorlieben, laufende Projekte …). Rufe dies SOFORT und STILL auf, wenn der "
                           "Nutzer etwas Persönliches preisgibt. Kündige das Speichern nicht an.",
            "parameters": {
                "type": "object",
                "properties": {"fact": {"type": "string", "description": "Der zu merkende Fakt, knapp formuliert"}},
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_user",
            "description": "Legt ein neues Nutzerprofil (passwortlos) an. Kein Admin nötig. Falls gerade eine "
                           "nicht erkannte Stimme vorliegt, wird sie automatisch als Stimmprofil hinterlegt. "
                           "Nutze dies, wenn ein unbekannter Sprecher ein neues Profil möchte (mit Namen).",
            "parameters": {
                "type": "object",
                "properties": {"username": {"type": "string", "description": "Name des neuen Nutzers"}},
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_my_voice",
            "description": "Fügt die AKTUELLE Sprachaufnahme dem Stimmprofil der gerade erkannten Person hinzu "
                           "und verbessert so die Erkennung (z.B. über ein neues Mikrofon/Satellit). "
                           "Nutze dies, wenn die Person sinngemäß sagt 'merke dir meine Stimme' / 'füge das meinem Profil hinzu'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "link_voice_to_existing_user",
            "description": "Ergänzt das Stimmprofil eines BEREITS REGISTRIERTEN Nutzers um die aktuelle, "
                           "nicht erkannte Sprachaufnahme. Nutze dies, wenn der unbekannte Sprecher sagt, dass "
                           "er bereits registriert ist, und seinen Namen nennt.",
            "parameters": {
                "type": "object",
                "properties": {"username": {"type": "string", "description": "Name des bestehenden Nutzers"}},
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Sendet eine Nachricht über den Messaging-Kanal (Telegram) — an den aktuellen Nutzer "
                           "oder, mit to_user, an einen bestimmten registrierten Nutzer (sofern dieser eine "
                           "Telegram-ID hinterlegt hat). Nutze dies, wenn der Nutzer um eine Nachricht/Benachrichtigung "
                           "bittet oder jemandem etwas ausgerichtet werden soll.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Nachrichtentext."},
                    "to_user": {"type": "string", "description": "Optional: Benutzername des Empfängers (sonst der aktuelle Nutzer)."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Ruft eine konkrete Webseite (URL) ab und extrahiert Titel, Überschriften und Lesetext. "
                           "Nutze dies, wenn du den INHALT einer bestimmten Seite brauchst (z.B. Nachrichten-Schlagzeilen "
                           "von einer Newsseite, Artikeltext) — im Gegensatz zu web_search, das nur kurze Treffer-Snippets "
                           "liefert. Fasse den Inhalt danach auf Deutsch zusammen.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Vollständige URL inkl. https://"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "research",
            "description": "Führt eine MEHRSTUFIGE Recherche durch: sucht im Web, ruft mehrere Quellen ab und "
                           "fasst das Ergebnis mit Quellenangaben zusammen. Nutze dies für gründliche/umfassende "
                           "Fragen, bei denen eine einzelne Suche/Seite nicht reicht (Hintergründe, Vergleiche, "
                           "aktuelle Lage). Gibt eine zusammengefasste Antwort inkl. Quellenliste zurück.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Forschungsfrage/Thema."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": "Analysiert ein Bild (multimodal) und beantwortet eine Frage dazu bzw. beschreibt es. "
                           "Nutze dies für Bild-URLs (z.B. aus einer Webseite) — gib die vollständige Bild-URL an.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_url": {"type": "string", "description": "Vollständige Bild-URL (https://…)."},
                    "question": {"type": "string", "description": "Was möchtest du über das Bild wissen? (optional)"},
                },
                "required": ["image_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_search",
            "description": "Durchsucht die hochgeladene Wissensbasis (Dokumente des Nutzers) nach relevanten "
                           "Stellen. Nutze dies bei Fragen, die sich auf eigene Dokumente/Unterlagen beziehen. "
                           "Fasse die Fundstellen danach auf Deutsch zusammen und nenne die Quelle.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Worum geht es? (Suchbegriff/Frage)"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_automation",
            "description": "Legt eine AUTONOME Aufgabe an, die JARVIS später von selbst ausführt — "
                           "zeitgesteuert (einmalig, in N Minuten, täglich/wöchentlich zu einer Uhrzeit) ODER "
                           "ereignisgesteuert (z.B. wenn eine bestimmte Person erkannt wird). Nutze dies bei "
                           "Wünschen wie „erinnere mich täglich um 7 an…“, „prüfe stündlich… und sag Bescheid wenn…“, "
                           "„wenn Daniel erkannt wird, begrüße ihn“. Die Aufgabe wird im Hintergrund mit Werkzeugen "
                           "ausgeführt und das Ergebnis genau an dieses Gerät gemeldet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Kurzer Name der Automatisierung."},
                    "task": {"type": "string", "description": "Klare Anweisung, was JARVIS bei Auslösung tun soll "
                             "(z.B. 'Hole das Wetter für Hamburg und fasse es in einem Satz zusammen'). "
                             "Soll nur bei Bedarf gemeldet werden, schreibe das mit hinein."},
                    "trigger_type": {"type": "string", "enum": ["once", "interval", "daily", "weekly", "event"],
                                     "description": "Art des Auslösers."},
                    "in_minutes": {"type": "integer", "description": "Für 'once': in so vielen Minuten ab jetzt."},
                    "at": {"type": "string", "description": "Für 'once': absoluter Zeitpunkt ISO 'YYYY-MM-DDTHH:MM'."},
                    "interval_minutes": {"type": "integer", "description": "Für 'interval': Abstand in Minuten."},
                    "time_of_day": {"type": "string", "description": "Für 'daily'/'weekly': Uhrzeit 'HH:MM'."},
                    "weekdays": {"type": "array", "items": {"type": "integer"},
                                 "description": "Für 'weekly': Wochentage, 0=Montag … 6=Sonntag."},
                    "event": {"type": "string", "enum": list(automations.KNOWN_EVENTS),
                              "description": "Für 'event': Ereignisname. Verfügbar: "
                              + ", ".join(f"{k} ({v['label']})" for k, v in automations.KNOWN_EVENTS.items())},
                    "event_match": {"type": "string", "description": "Für 'event': optionaler Filter, z.B. ein "
                                    "Benutzername bei speaker_recognized."},
                },
                "required": ["title", "task", "trigger_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_automations",
            "description": "Listet die eingerichteten autonomen Aufgaben des aktuellen Nutzers (Titel, Auslöser, "
                           "nächste Ausführung).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_automation",
            "description": "Ändert eine bestehende autonome Aufgabe — z.B. um einen Fehler im Aufgabentext zu "
                           "korrigieren. Nutze dies, wenn der Nutzer auf einen Fehler in einer wiederkehrenden "
                           "Aufgabe hinweist (Beispiel: „die heisse.de gibt es nicht, das schreibt man mit einem s“ "
                           "→ Aufgabentext der betroffenen Automatisierung mit korrigierter Schreibweise neu setzen). "
                           "Rufe ggf. zuerst list_automations auf, um Titel/ID und aktuellen Text zu sehen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "description": "Titel oder ID der Automatisierung."},
                    "new_task": {"type": "string", "description": "Neuer, vollständiger Aufgabentext (ersetzt den alten)."},
                    "new_title": {"type": "string", "description": "Optional: neuer Titel."},
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_automation",
            "description": "Löscht/deaktiviert eine autonome Aufgabe per Titel oder ID.",
            "parameters": {
                "type": "object",
                "properties": {"identifier": {"type": "string", "description": "Titel oder ID der Automatisierung."}},
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Führt Python-Code in einer ISOLIERTEN Sandbox aus (eigener Container, nicht der Host) "
                           "und gibt stdout/stderr + erzeugte Dateien zurück. Nutze dies für Berechnungen, "
                           "Datenverarbeitung, Datei-/Diagramm-/PDF-Erzeugung oder um dir selbst Helfer-Skripte zu "
                           "schreiben. Verfügbar u.a.: requests, pandas, numpy, matplotlib, openpyxl, reportlab, pillow. "
                           "Dateien im aktuellen Verzeichnis bleiben für spätere Läufe erhalten. Gib Ergebnisse mit print() aus.",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string", "description": "Auszuführender Python-Code."}},
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Führt einen Shell-Befehl (bash) in der isolierten Sandbox aus und gibt die Ausgabe zurück. "
                           "Für Datei-/Systemoperationen im Workspace. Kein Zugriff auf den Host.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "Shell-Befehl."}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse",
            "description": "Öffnet eine Webseite in einem ECHTEN headless-Browser (Chromium) und gibt den "
                           "gerenderten Text + Links zurück. Im Gegensatz zu fetch_url werden JavaScript-Seiten "
                           "voll gerendert. Die Browser-Sitzung bleibt offen (Cookies/Logins bleiben erhalten), "
                           "sodass du mit browser_click/browser_type weiter interagieren kannst.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Vollständige URL inkl. https://"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Klickt im offenen Browser auf ein Element mit dem angegebenen sichtbaren Text "
                           "(Link/Button). Gibt die aktualisierte Seite zurück.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Sichtbarer Text des Links/Buttons."}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Tippt Text in ein Eingabefeld (per Label/Platzhalter) im offenen Browser — z.B. für "
                           "Suchfelder oder Logins. Mit submit=true wird danach Enter gedrückt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "Label/Platzhalter/Name des Feldes."},
                    "value": {"type": "string", "description": "Einzugebender Text."},
                    "submit": {"type": "boolean", "description": "Nach der Eingabe Enter drücken?"},
                },
                "required": ["field", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Macht einen Screenshot der aktuellen Browser-Seite und analysiert ihn multimodal "
                           "(z.B. um visuelle Inhalte/Layout zu verstehen). Optionale Frage dazu.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string", "description": "Was möchtest du über die Seite wissen? (optional)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workspace_files",
            "description": "Listet die Dateien im Sandbox-Workspace des aktuellen Nutzers (z.B. zuvor erzeugte Dateien).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_workspace_file",
            "description": "Liest eine Datei aus dem Sandbox-Workspace (Text).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Pfad relativ zum Workspace."}},
                "required": ["path"],
            },
        },
    },
]


async def execute_tool(name: str, args: dict, ctx: dict) -> str:
    """Wrapper mit Debug-Aufzeichnung (Tool-Name, Argumente, Ergebnis, Dauer)."""
    t0 = time.time()
    result = await _execute_tool_impl(name, args, ctx)
    debug.log("tool", name=name, args=args, result=str(result)[:400],
              ms=int((time.time() - t0) * 1000), user_id=ctx.get("user_id"))
    return result


async def _execute_tool_impl(name: str, args: dict, ctx: dict) -> str:
    """Führt das Tool aus und gibt einen kurzen, deutschen Ergebnis-String zurück
    (geht als 'tool'-Message zurück ans LLM)."""
    sid = ctx.get("session_id") or "anon"
    ns = ctx.get("namespace", "default")     # pro Nutzer/Sprecher
    args = args or {}

    # MCP-Tools (mcp__<server>__<tool>) → Ressource ist mcp:<server>
    is_mcp = name.startswith("mcp__")
    mcp_server = name.split("__", 2)[1] if is_mcp else None
    resource = f"mcp:{mcp_server}" if is_mcp else f"tool:{name}"

    # Autonomie-Blacklist: bei selbstständigen (geplanten/ereignis-)Läufen sind bestimmte
    # Tools/MCP-Server gesperrt — unabhängig von den sonstigen Rechten des Besitzers.
    if ctx.get("autonomous"):
        cfg = ctx.get("cfg") or config.get()
        if (mcp_server and mcp_server in (cfg.get("autonomous_mcp_blacklist") or [])) or \
           (not is_mcp and name in (cfg.get("autonomous_tool_blacklist") or [])):
            return (f"Autonom gesperrt: „{mcp_server or name}“ darf bei selbstständigen Läufen "
                    "nicht verwendet werden (Admin-Blacklist).")

    # Autorisierung (Defense-in-Depth — der Loop bietet verbotene Tools schon nicht an)
    try:
        if not await asyncio.to_thread(auth.is_tool_allowed, ctx.get("user_id"), resource):
            who = mcp_server or name
            return f"Berechtigung verweigert: Für „{who}“ fehlt dir die Freigabe."
    except Exception:
        pass

    if is_mcp:
        _, server, tool = name.split("__", 2)
        return await mcp_hub.call_tool(server, tool, args)

    if name == "set_timer":
        dur = int(args.get("duration_seconds", 0) or 0)
        if dur <= 0:
            return "Fehler: Dauer fehlt oder ist ungültig."
        label = (args.get("label") or "").strip()
        info = timers.manager.add(sid, dur, label)
        return f"Timer '{info['label']}' über {timers.format_duration(dur)} gesetzt."

    if name == "list_timers":
        active = timers.manager.list(sid)
        if not active:
            return "Es laufen aktuell keine Timer."
        return "Laufende Timer: " + "; ".join(
            f"{t['label']} (noch {timers.format_duration(t['remaining'])})" for t in active
        )

    if name == "cancel_timer":
        ident = (args.get("identifier") or "").strip()
        cancelled = timers.manager.cancel(sid, ident)
        return f"Timer '{cancelled['label']}' abgebrochen." if cancelled else f"Kein Timer '{ident}' gefunden."

    if name == "create_automation":
        return _create_automation(args, ctx, sid)

    if name == "list_automations":
        items = automations.manager.list(ctx.get("user_id"))
        if not items:
            return "Es sind keine autonomen Aufgaben eingerichtet."
        return "Autonome Aufgaben: " + "; ".join(
            f"{a['title']} ({automations.trigger_summary(a['trigger'])}"
            + (f", nächste {time.strftime('%d.%m. %H:%M', time.localtime(a['next_run']))}" if a.get('next_run') else "")
            + (")" if a.get("enabled") else ", deaktiviert)")
            for a in items)

    if name == "update_automation":
        a = automations.manager.find(ctx.get("user_id"), (args.get("identifier") or "").strip())
        if not a:
            return f"Keine Automatisierung „{args.get('identifier')}“ gefunden."
        fields = {}
        if args.get("new_task"):
            fields["task"] = str(args["new_task"]).strip()
        if args.get("new_title"):
            fields["title"] = str(args["new_title"]).strip()
        if not fields:
            return "Bitte gib an, was geändert werden soll (new_task und/oder new_title)."
        automations.manager.update(a["id"], **fields)
        return f"Automatisierung „{a['title']}“ aktualisiert."

    if name == "cancel_automation":
        a = automations.manager.find(ctx.get("user_id"), (args.get("identifier") or "").strip())
        if not a:
            return f"Keine Automatisierung „{args.get('identifier')}“ gefunden."
        automations.manager.delete(a["id"])
        return f"Automatisierung „{a['title']}“ gelöscht."

    if name in ("run_python", "run_shell"):
        code = (args.get("code") if name == "run_python" else args.get("command")) or ""
        if not code.strip():
            return "Fehler: Kein Code/Befehl angegeben."
        lang = "python" if name == "run_python" else "shell"
        res = await asyncio.to_thread(sandbox.execute, code, lang, ns)
        return _format_sandbox_result(res)

    if name == "browse":
        url = (args.get("url") or "").strip()
        if not url:
            return "Fehler: Keine URL."
        if not _url_is_safe(url):
            return "Diese URL ist nicht erlaubt (nur öffentliche http/https-Adressen)."
        return _format_browse(await asyncio.to_thread(sandbox.browser_goto, ns, url))

    if name == "browser_click":
        return _format_browse(await asyncio.to_thread(
            sandbox.browser_act, ns, "click", (args.get("text") or "").strip()))

    if name == "browser_type":
        field = (args.get("field") or "").strip()
        if not field:
            return "Fehler: Kein Feld angegeben."
        return _format_browse(await asyncio.to_thread(
            sandbox.browser_act, ns, "type", field, (args.get("value") or ""), bool(args.get("submit"))))

    if name == "browser_screenshot":
        shot = await asyncio.to_thread(sandbox.browser_screenshot, ns)
        if not shot.get("ok"):
            return f"Screenshot fehlgeschlagen: {shot.get('error', 'unbekannt')}"
        try:
            return await asyncio.to_thread(services.vision_call,
                                           (args.get("question") or "").strip(), shot["image"], ctx["cfg"])
        except Exception as e:
            return f"Screenshot-Analyse fehlgeschlagen: {e}"

    if name == "list_workspace_files":
        files = await asyncio.to_thread(sandbox.list_files, ns)
        if not files:
            return "Der Workspace ist leer."
        return "Dateien im Workspace: " + ", ".join(f"{f['path']} ({f['bytes']} B)" for f in files)

    if name == "read_workspace_file":
        path = (args.get("path") or "").strip()
        res = await asyncio.to_thread(sandbox.read_file, ns, path)
        if not res.get("ok"):
            return f"Datei nicht lesbar: {res.get('error', 'unbekannt')}"
        return f"Inhalt von {path}:\n{res['content']}"

    if name == "set_device_volume":
        try:
            level = int(args.get("level", 0))
        except (TypeError, ValueError):
            level = 0
        if not 1 <= level <= 10:
            return "Bitte eine Lautstärke zwischen 1 und 10 angeben."
        # An die Ursprungs-Session pushen — das Gerät setzt die Lautstärke lokal (mit eigenem Cap).
        await hub.push(sid, {"type": "set_volume", "level": level})
        return f"Lautstärke auf {level} gesetzt."

    if name == "list_clients":
        cs = hub.clients()
        if not cs:
            return "Es sind aktuell keine Client-Rechner verbunden."
        return "Verbundene Client-Rechner: " + "; ".join(
            f"{c.get('name', '?')} ({len(c.get('capabilities', []))} Aktionen)" for c in cs)

    if name == "list_client_capabilities":
        device = (args.get("device") or "").strip() or None
        target = hub.resolve_target_client(sid, device)
        if not target:
            return _no_client_msg(device)
        caps = hub.capabilities(target)
        nm = hub.meta(target).get("name", target)
        return f"Client „{nm}“ kann: {', '.join(caps)}." if caps else f"Client „{nm}“ meldet keine Aktionen."

    if name == "client_screenshot":
        device = (args.get("device") or "").strip() or None
        target = hub.resolve_target_client(sid, device)
        if not target:
            return _no_client_msg(device)
        if "screenshot" not in hub.capabilities(target):
            return "Der Client erlaubt keine Screenshots (Policy)."
        res = await hub.call_client(target, "screenshot", {}, timeout=40)
        if not res.get("ok"):
            return f"Screenshot fehlgeschlagen: {res.get('error', 'unbekannt')}"
        data_uri = res.get("result") or ""
        if not isinstance(data_uri, str) or not data_uri.startswith("data:image"):
            return "Screenshot lieferte kein Bild."
        try:
            return await asyncio.to_thread(services.vision_call,
                                           (args.get("question") or "").strip(), data_uri, ctx["cfg"])
        except Exception as e:
            return f"Screenshot-Analyse fehlgeschlagen: {e}"

    if name == "client_action":
        action = (args.get("action") or "").strip()
        if not action:
            return "Fehler: Keine Aktion angegeben."
        device = (args.get("device") or "").strip() or None
        target = hub.resolve_target_client(sid, device)
        if not target:
            return _no_client_msg(device)
        caps = hub.capabilities(target)
        if caps and action not in caps:
            return f"Diese Aktion kann der Client nicht ({action}). Verfügbar: {', '.join(caps)}."
        res = await hub.call_client(target, action, args.get("params") or {})
        if not res.get("ok"):
            return f"Aktion fehlgeschlagen: {res.get('error', 'unbekannt')}"
        out = res.get("result")
        return f"Erledigt: {out}" if out not in (None, "", {}) else "Erledigt."

    if name == "get_datetime":
        return _get_datetime()

    if name == "weather":
        loc = (args.get("location") or "").strip()
        if not loc:
            return "Fehler: Kein Ort angegeben."
        return await asyncio.to_thread(_weather, loc)

    if name == "web_search":
        query = (args.get("query") or "").strip()
        if not query:
            return "Fehler: Keine Suchanfrage."
        n = int(args.get("max_results", 5) or 5)
        return await asyncio.to_thread(_web_search, query, n)

    if name == "save_memory":
        fact = (args.get("fact") or "").strip()
        if not fact:
            return "Fehler: Kein Fakt angegeben."
        try:
            await asyncio.to_thread(knowledge.save_memory, ctx["cfg"], fact, ns)
            automations.emit("memory_saved", {"namespace": ns})
            return "Gespeichert."
        except Exception as e:
            return f"Speichern fehlgeschlagen: {e}"

    if name == "create_user":
        uname = (args.get("username") or "").strip()
        if not uname:
            return "Kein Benutzername angegeben."
        if await asyncio.to_thread(auth.user_by_name, uname):
            return f"Es gibt bereits einen Nutzer namens „{uname}“."
        try:
            new_id = await asyncio.to_thread(auth.create_user, uname)     # passwortlos, kein Admin nötig
        except Exception as e:
            return f"Anlegen fehlgeschlagen: {e}"
        automations.emit("user_created", {"username": uname, "user_id": new_id})
        emb = hub.get_last_voice(sid)
        msg = f"Profil „{uname}“ wurde angelegt (Passwort vergibst du beim ersten Login selbst)."
        if emb is not None:
            try:
                await asyncio.to_thread(biometrics.add_voiceprint, new_id, emb)
                hub.set_identity(sid, {"user_id": new_id, "username": uname, "confidence": 1.0})
                msg += " Deine Stimme habe ich gespeichert — beim nächsten Mal erkenne ich dich."
            except Exception:
                pass
        return msg

    if name == "remember_my_voice":
        uid = ctx.get("user_id")
        if not uid:
            return ("Ich bin mir noch nicht sicher, wer du bist. Sag mir bitte deinen Namen, "
                    "dann kann ich diese Stimmaufnahme zuordnen.")
        emb = hub.get_last_voice(sid)
        if emb is None:
            return "Mir liegt gerade keine Sprachaufnahme vor."
        try:
            await asyncio.to_thread(biometrics.add_voiceprint, uid, emb)
            automations.emit("voice_enrolled", {"username": (hub.get_identity(sid) or {}).get("username"),
                                                "user_id": uid})
            return "Erledigt, ich habe diese Stimmaufnahme deinem Profil hinzugefügt."
        except Exception as e:
            return f"Konnte die Stimme nicht hinterlegen: {e}"

    if name == "link_voice_to_existing_user":
        uname = (args.get("username") or "").strip()
        u = await asyncio.to_thread(auth.user_by_name, uname)
        if not u:
            return f"Ich finde keinen registrierten Nutzer namens „{uname}“."
        emb = hub.get_last_voice(sid)
        if emb is None:
            return "Mir liegt gerade keine Sprachaufnahme vor, die ich hinterlegen könnte."
        try:
            await asyncio.to_thread(biometrics.add_voiceprint, u["id"], emb)
            hub.set_identity(sid, {"user_id": u["id"], "username": u["username"], "confidence": 1.0})
            automations.emit("voice_enrolled", {"username": u["username"], "user_id": u["id"]})
            return f"Alles klar, ich habe deine Stimme dem Profil „{u['username']}“ hinzugefügt — willkommen zurück."
        except Exception as e:
            return f"Konnte die Stimme nicht hinterlegen: {e}"

    if name == "send_message":
        text = (args.get("text") or "").strip()
        if not text:
            return "Fehler: Kein Nachrichtentext."
        if not messaging.enabled():
            return "Der Messaging-Kanal (Telegram) ist nicht konfiguriert/aktiv."
        to_user = (args.get("to_user") or "").strip()
        if to_user:
            u = await asyncio.to_thread(auth.user_by_name, to_user)
            if not u:
                return f"Ich finde keinen Nutzer namens „{to_user}“."
            ok = await asyncio.to_thread(messaging.send_to_user, u["id"], text)
            return f"Nachricht an {to_user} gesendet." if ok else \
                   f"Konnte {to_user} nicht erreichen (keine Telegram-ID hinterlegt?)."
        uid = ctx.get("user_id")
        if uid is None:
            return "Ich weiß nicht, wem ich schreiben soll — ich erkenne dich gerade nicht."
        ok = await asyncio.to_thread(messaging.send_to_user, uid, text)
        return "Nachricht gesendet." if ok else "Konnte die Nachricht nicht senden (Telegram-ID hinterlegt?)."

    if name == "fetch_url":
        url = (args.get("url") or "").strip()
        if not url:
            return "Fehler: Keine URL angegeben."
        return await asyncio.to_thread(_fetch_url, url)

    if name == "research":
        query = (args.get("query") or "").strip()
        if not query:
            return "Fehler: Keine Forschungsfrage."
        return await asyncio.to_thread(_research, query, ctx["cfg"])

    if name == "analyze_image":
        image_url = (args.get("image_url") or "").strip()
        if not image_url:
            return "Fehler: Keine Bild-URL."
        # GPU-Server hat kein Internet → Bild im Orchestrator laden und als data-URI senden.
        if image_url.startswith("http"):
            data_uri, err = await asyncio.to_thread(_fetch_image_data_uri, image_url)
            if err:
                return f"Bild konnte nicht geladen werden: {err}"
            image_url = data_uri
        try:
            return await asyncio.to_thread(services.vision_call,
                                           (args.get("question") or "").strip(), image_url, ctx["cfg"])
        except Exception as e:
            return f"Bildanalyse fehlgeschlagen: {e}"

    if name == "knowledge_search":
        query = (args.get("query") or "").strip()
        if not query:
            return "Fehler: Keine Suchanfrage."
        try:
            hits = await asyncio.to_thread(knowledge.search_knowledge, ctx["cfg"], query, ns)
        except Exception as e:
            return f"Wissens-Suche fehlgeschlagen: {e}"
        if not hits:
            return "Keine passenden Stellen in der Wissensbasis gefunden."
        return "\n".join(f"[{h['source']}] {h['content']}" for h in hits)

    return f"Unbekanntes Tool: {name}"


def _url_is_safe(url: str) -> bool:
    """SSRF-Schutz: nur http(s) zu öffentlichen Adressen — keine internen/lokalen Ziele."""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        for fam, _, _, _, sockaddr in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def _fetch_url(url: str, max_chars: int = 4000) -> str:
    """Webseite laden und Titel/Überschriften/Lesetext extrahieren (lxml)."""
    if not _url_is_safe(url):
        return "Diese URL ist nicht erlaubt (nur öffentliche http/https-Adressen, keine internen Ziele)."
    try:
        r = requests.get(url, timeout=12, headers={
            "User-Agent": "Mozilla/5.0 (compatible; JARVIS/1.0; +https://heise.de)",
            "Accept-Language": "de,en;q=0.8",
        }, stream=True)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype and "text" not in ctype:
            return f"Inhalt ist kein Text/HTML ({ctype or 'unbekannt'})."
        raw = r.raw.read(2_000_000, decode_content=True)        # max ~2 MB
        html = raw.decode(r.encoding or "utf-8", "replace")
    except Exception as e:
        return f"Abruf fehlgeschlagen: {e}"
    try:
        from lxml import html as lxml_html
        doc = lxml_html.fromstring(html)
        for bad in doc.xpath("//script | //style | //noscript | //nav | //footer | //aside"):
            bad.getparent().remove(bad)
        title = (doc.findtext(".//title") or "").strip()
        heads = [h.text_content().strip() for h in doc.xpath("//h1 | //h2 | //h3")]
        heads = [h for h in heads if h][:20]
        text = " ".join(doc.text_content().split())
    except Exception as e:
        return f"HTML-Auswertung fehlgeschlagen: {e}"
    out = []
    if title:
        out.append(f"Titel: {title}")
    if heads:
        out.append("Überschriften:\n- " + "\n- ".join(heads))
    if text:
        out.append("Textauszug:\n" + text[:max_chars])
    return "\n\n".join(out) or "Keine verwertbaren Inhalte gefunden."


def _no_client_msg(device: str | None) -> str:
    """Hilfreiche Meldung, wenn kein eindeutiger Ziel-Client bestimmt werden konnte."""
    cs = hub.clients()
    names = [c.get("name", "?") for c in cs]
    if not cs:
        return "Es ist aktuell kein Client-Rechner verbunden."
    if device:
        return (f"Keinen Client-Rechner namens „{device}“ gefunden. "
                f"Verbunden sind: {', '.join(names)}.")
    return (f"Es sind mehrere Client-Rechner verbunden: {', '.join(names)}. "
            "Bitte sag, auf welchem die Aktion laufen soll.")


def _format_browse(d: dict) -> str:
    """Browser-Ergebnis kompakt fürs LLM."""
    if not d.get("ok"):
        return f"Browser-Fehler: {d.get('error', 'unbekannt')}"
    parts = [f"Seite: {d.get('title','')} ({d.get('url','')})"]
    if d.get("text"):
        parts.append(d["text"][:3000])
    links = d.get("links") or []
    if links:
        parts.append("Links: " + " · ".join(f"{l['text']}" for l in links[:15]))
    return "\n\n".join(parts)


def _fetch_image_data_uri(url: str, max_bytes: int = 8_000_000) -> tuple[str | None, str | None]:
    """Bild öffentlich laden (SSRF-Schutz) und als base64 data-URI zurückgeben — der GPU-Server
    hat kein Internet und kann externe Bild-URLs nicht selbst abrufen."""
    if not _url_is_safe(url):
        return None, "URL nicht erlaubt (nur öffentliche http/https)."
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (JARVIS)"}, stream=True)
        r.raise_for_status()
        ctype = (r.headers.get("content-type") or "").split(";")[0]
        if "image" not in ctype:
            return None, f"kein Bild ({ctype or 'unbekannt'})"
        data = r.raw.read(max_bytes + 1, decode_content=True)
        if len(data) > max_bytes:
            return None, "Bild zu groß (>8 MB)."
        import base64
        return f"data:{ctype};base64," + base64.b64encode(data).decode(), None
    except Exception as e:
        return None, str(e)


def _web_search_results(query: str, n: int = 6) -> list[dict]:
    """Strukturierte Suchtreffer (title/body/href) — für den Recherche-Agenten."""
    try:
        from ddgs import DDGS
        for attempt in range(3):
            try:
                res = list(DDGS().text(query, max_results=max(1, min(10, n)), region="de-de"))
                if res:
                    return res
            except Exception:
                pass
            time.sleep(0.5 * (attempt + 1))
    except Exception:
        pass
    return []


def _research(query: str, cfg: dict, max_fetch: int = 4) -> str:
    """Mehrstufige Recherche: Web-Suche → mehrere Quellen abrufen → mit Quellen zusammenfassen."""
    results = _web_search_results(query, 6)
    if not results:
        return "Ich habe keine Quellen zu dieser Frage gefunden."
    sources, corpus, fetched = [], [], 0
    for r in results[:6]:
        url = (r.get("href") or "").strip()
        title = (r.get("title") or url).strip()
        snippet = (r.get("body") or "")[:300]
        text = ""
        if url and fetched < max_fetch and _url_is_safe(url):
            got = _fetch_url(url, max_chars=1500)
            if not got.startswith(("Diese URL", "Abruf fehlgeschlagen", "Inhalt ist kein", "HTML-Auswertung")):
                text = got
                fetched += 1
        idx = len(sources) + 1
        sources.append({"i": idx, "title": title, "url": url})
        corpus.append(f"[{idx}] {title} ({url})\n{text or snippet}")
    sys = ("Du bist ein gründlicher Rechercheassistent. Beantworte die Frage AUSSCHLIESSLICH auf Deutsch, "
           "sachlich und kompakt, und belege Aussagen mit Quellennummern in eckigen Klammern [n]. "
           "Nutze NUR die bereitgestellten Quellen; wenn etwas unklar oder widersprüchlich ist, benenne das.")
    user = f"Frage: {query}\n\nQuellen:\n" + "\n\n".join(corpus)
    try:
        res = services.llm_call([{"role": "system", "content": sys}, {"role": "user", "content": user}],
                                cfg, None, think=False)
        answer = (res.get("content") or "").strip()
    except Exception as e:
        return f"Recherche-Synthese fehlgeschlagen: {e}"
    src_list = "\n".join(f"[{s['i']}] {s['url']}" for s in sources if s["url"])
    return (answer or "Keine belastbare Antwort möglich.") + (f"\n\nQuellen:\n{src_list}" if src_list else "")


def _create_automation(args: dict, ctx: dict, sid: str) -> str:
    """Übersetzt die flachen LLM-Argumente in einen Trigger und legt die Automatisierung an."""
    tt = (args.get("trigger_type") or "").strip()
    trigger: dict = {"type": tt}
    try:
        if tt == "once":
            if args.get("in_minutes") is not None:
                trigger["at"] = time.time() + int(args["in_minutes"]) * 60
            elif args.get("at"):
                from datetime import datetime
                trigger["at"] = datetime.fromisoformat(str(args["at"])).timestamp()
            else:
                return "Für einen einmaligen Auslöser brauche ich einen Zeitpunkt (in_minutes oder at)."
            if trigger["at"] <= time.time() + 1:
                return ("Der angegebene Zeitpunkt liegt in der Vergangenheit. Prüfe das aktuelle Datum "
                        "und nutze einen künftigen Zeitpunkt — oder, wenn es regelmäßig (z.B. täglich) zu "
                        "einer Uhrzeit laufen soll, trigger_type='daily' mit time_of_day.")
        elif tt == "interval":
            mins = int(args.get("interval_minutes") or 0)
            if mins <= 0:
                return "Für einen Intervall-Auslöser brauche ich interval_minutes (> 0)."
            trigger["seconds"] = mins * 60
        elif tt in ("daily", "weekly"):
            trigger["time"] = (args.get("time_of_day") or "").strip()
            if not trigger["time"]:
                return "Bitte eine Uhrzeit (time_of_day, z.B. '07:00') angeben."
            if tt == "weekly":
                trigger["weekdays"] = [int(d) for d in (args.get("weekdays") or [])] or list(range(7))
        elif tt == "event":
            ev = (args.get("event") or "").strip()
            if not ev:
                return "Für einen Ereignis-Auslöser brauche ich einen Ereignisnamen (event)."
            trigger["event"] = ev
            if args.get("event_match"):
                trigger["match"] = str(args["event_match"]).strip()
        else:
            return f"Unbekannter Auslöser-Typ: {tt}"
    except (ValueError, TypeError) as e:
        return f"Auslöser-Angaben ungültig: {e}"

    a = automations.manager.create(
        title=(args.get("title") or "").strip(),
        task=(args.get("task") or "").strip(),
        trigger=trigger,
        owner_user_id=ctx.get("user_id"),
        target_session=sid,
    )
    return (f"Automatisierung „{a['title']}“ angelegt ({automations.trigger_summary(trigger)}). "
            "Ich kümmere mich von selbst darum.")


def _format_sandbox_result(res: dict) -> str:
    """Sandbox-Ergebnis kompakt fürs LLM aufbereiten."""
    if res.get("disabled"):
        return "Die Code-Ausführung ist derzeit deaktiviert (Admin-Einstellung)."
    if res.get("offline"):
        return res.get("stderr", "Die Sandbox ist nicht erreichbar.")
    parts = []
    if res.get("timed_out"):
        parts.append("[Zeitüberschreitung — Ausführung abgebrochen]")
    out = (res.get("stdout") or "").strip()
    err = (res.get("stderr") or "").strip()
    if out:
        parts.append("Ausgabe:\n" + out)
    if err:
        parts.append("Fehler/Logs:\n" + err)
    files = res.get("files") or []
    if files:
        parts.append("Erzeugte Dateien: " + ", ".join(f"{f['path']} ({f['bytes']} B)" for f in files))
    if not parts:
        parts.append(f"(keine Ausgabe, Exit-Code {res.get('exit_code')})")
    return "\n\n".join(parts)


# ── Tool-Implementierungen ────────────────────────────────────────────────────

_WMO = {
    0: "klar", 1: "überwiegend klar", 2: "teils bewölkt", 3: "bedeckt",
    45: "Nebel", 48: "gefrierender Nebel",
    51: "leichter Nieselregen", 53: "Nieselregen", 55: "starker Nieselregen",
    61: "leichter Regen", 63: "Regen", 65: "starker Regen",
    66: "gefrierender Regen", 67: "starker gefrierender Regen",
    71: "leichter Schneefall", 73: "Schneefall", 75: "starker Schneefall", 77: "Schneekörner",
    80: "leichte Regenschauer", 81: "Regenschauer", 82: "heftige Regenschauer",
    85: "Schneeschauer", 86: "starke Schneeschauer",
    95: "Gewitter", 96: "Gewitter mit Hagel", 99: "schweres Gewitter mit Hagel",
}


def _get_datetime() -> str:
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Europe/Berlin"))
    except Exception:
        now = datetime.now()
    tage = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    monate = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
              "August", "September", "Oktober", "November", "Dezember"]
    return (f"Es ist {tage[now.weekday()]}, der {now.day}. {monate[now.month - 1]} "
            f"{now.year}, {now:%H:%M} Uhr.")


def _weather(location: str) -> str:
    try:
        g = _http_json("https://geocoding-api.open-meteo.com/v1/search",
                       {"name": location, "count": 1, "language": "de"})
        results = g.get("results")
        if not results:
            return f"Ort '{location}' wurde nicht gefunden."
        r = results[0]
        name, country = r["name"], r.get("country", "")
        w = _http_json("https://api.open-meteo.com/v1/forecast", {
            "latitude": r["latitude"], "longitude": r["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code",
            "timezone": "auto",
        })
        c = w.get("current", {})
        desc = _WMO.get(c.get("weather_code"), "wechselhaft")
        return (f"Wetter in {name}{', ' + country if country else ''}: {desc}, "
                f"{c.get('temperature_2m')}°C (gefühlt {c.get('apparent_temperature')}°C), "
                f"Luftfeuchte {c.get('relative_humidity_2m')}%, Wind {c.get('wind_speed_10m')} km/h.")
    except Exception as e:
        return f"Wetterabruf fehlgeschlagen: {e}"


def _web_search(query: str, max_results: int = 5) -> str:
    try:
        from ddgs import DDGS
        n = max(1, min(10, max_results))
        results: list = []
        for attempt in range(3):
            try:
                results = list(DDGS().text(query, max_results=n, region="de-de"))
                if results:
                    break
            except Exception:
                pass
            time.sleep(0.5 * (attempt + 1))
        if not results:
            return "Keine Treffer gefunden."
        out = []
        for i, r in enumerate(results, 1):
            out.append(f"{i}. {r.get('title', '')}\n   {(r.get('body') or '')[:220]}\n   {r.get('href', '')}")
        return "\n".join(out)
    except Exception as e:
        return f"Web-Suche fehlgeschlagen: {e}"
