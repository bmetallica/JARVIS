"""
Tool-Layer des Orchestrators.

TOOL_SCHEMAS  — OpenAI-Function-Definitionen, die dem LLM mitgegeben werden.
execute_tool  — führt ein Tool aus; bekommt einen Kontext (u.a. session_id),
                damit Ausgaben/Alarme an die Ursprungsquelle gebunden bleiben.

Erweiterbar: neue interne Tools (web_search, RAG, Wetter …) hier ergänzen.
"""
from __future__ import annotations

import asyncio
import base64
import time
from datetime import date, datetime, timedelta, timezone

import requests

import auth
import automations
import biometrics
import calendars
import config
import debug
import knowledge
import mcp_hub
import messaging
import obsidian
import sandbox
import services
import skills
import timers
import todos
import watchers
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
            "name": "add_todo",
            "description": "Setzt einen Punkt auf die persönliche To-do-Liste des Nutzers ('schreibe … auf meine "
                           "To-do', 'notiere als Aufgabe …'). Mit Datum (ISO, optional) erscheint er zusätzlich im "
                           "Kalender. Datum aus relativen Angaben ('nächste Woche Mittwoch') anhand des Datums oben.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"},
                               "due": {"type": "string", "description": "Fälligkeitsdatum YYYY-MM-DD (optional)."}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_todos",
            "description": "Listet die To-do-Punkte des Nutzers ('was ist auf meiner To-do', 'welche Punkte stehen "
                           "an'). scope: open (Standard) | all | done.",
            "parameters": {"type": "object",
                           "properties": {"scope": {"type": "string", "description": "open|all|done"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_todo",
            "description": "Markiert einen To-do-Punkt als ERLEDIGT. Der Punkt wird per Ähnlichkeitssuche gefunden "
                           "('Äpfel holen ist erledigt'). Optional auf ein Datum eingrenzen (due, YYYY-MM-DD).",
            "parameters": {
                "type": "object",
                "properties": {"item": {"type": "string", "description": "Ungefähre Beschreibung des Punkts."},
                               "due": {"type": "string", "description": "Datum zur Eingrenzung (optional)."}},
                "required": ["item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_todo",
            "description": "Löscht einen To-do-Punkt (per Ähnlichkeitssuche). Optional auf ein Datum eingrenzen.",
            "parameters": {
                "type": "object",
                "properties": {"item": {"type": "string"}, "due": {"type": "string"}},
                "required": ["item"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "todo_link",
            "description": "Nennt den Hardlink zur smartphone-optimierten To-do-Liste des Nutzers (ohne Login abhakbar).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "Speichert eine NOTIZ in der persönlichen Obsidian-Vault des Nutzers. Nutze dies, wenn "
                           "der Nutzer ausdrücklich eine Notiz festhalten will (z. B. 'Notiz: …', 'schreib in "
                           "Obsidian …', 'notiere …'). Für reine persönliche FAKTEN über den Nutzer stattdessen "
                           "save_memory verwenden.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Inhalt der Notiz."},
                    "title": {"type": "string", "description": "Optionaler Titel → eigene Note <Titel>.md; "
                                                               "ohne Titel wird die Notiz an die Inbox angehängt."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_event",
            "description": "Trägt einen TERMIN in einen Kalender ein. Zeiten als ISO 8601 in LOKALER Zeit "
                           "(Europe/Berlin), z. B. '2026-06-26T15:00'. Nutze das Datum/die Uhrzeit oben im Prompt, "
                           "um relative Angaben ('morgen 15 Uhr') umzurechnen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "Start, ISO 8601 lokal (bei ganztägig: YYYY-MM-DD)."},
                    "end": {"type": "string", "description": "Ende (optional; Standard +1 Std)."},
                    "calendar": {"type": "string", "description": "'own' (eigener, Standard), 'common' (gemeinsam) "
                                                                  "oder ein Nutzername (dessen Kalender, falls freigegeben)."},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "all_day": {"type": "boolean"},
                    "recurrence": {"type": "string", "description": "none|daily|weekly|monthly|yearly"},
                },
                "required": ["title", "start"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_events",
            "description": "Listet anstehende Termine über alle zugänglichen Kalender (oder einen bestimmten). "
                           "Zeitfenster optional als ISO-Datum; Standard: nächste 7 Tage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from": {"type": "string", "description": "Startdatum (ISO, optional)."},
                    "to": {"type": "string", "description": "Enddatum (ISO, optional)."},
                    "calendar": {"type": "string", "description": "'own'|'common'|Nutzername (optional, sonst alle)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_event",
            "description": "Ändert einen Termin (per event_id aus list_events). Nur Ersteller/Kalenderbesitzer/Admin.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer"},
                    "title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
                    "description": {"type": "string"}, "location": {"type": "string"},
                    "all_day": {"type": "boolean"}, "recurrence": {"type": "string"},
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_event",
            "description": "Löscht/trägt einen Termin aus (per event_id). Nur Ersteller/Kalenderbesitzer/Admin.",
            "parameters": {"type": "object", "properties": {"event_id": {"type": "integer"}}, "required": ["event_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "share_calendar",
            "description": "Gibt den EIGENEN Kalender für einen anderen JARVIS-Nutzer frei.",
            "parameters": {
                "type": "object",
                "properties": {"username": {"type": "string"},
                               "access": {"type": "string", "description": "read (Standard) | write"}},
                "required": ["username"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unshare_calendar",
            "description": "Entzieht einem Nutzer die Freigabe für den eigenen Kalender.",
            "parameters": {"type": "object", "properties": {"username": {"type": "string"}}, "required": ["username"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_subscription",
            "description": "Nennt die iCal-Abo-Links (nur im LAN) des Nutzers — für Kalender-Apps wie Apple "
                           "Kalender, Thunderbird oder DAVx5.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "subscribe_calendar",
            "description": "ABONNIERT einen EXTERNEN iCal-Kalender (URL, z. B. Google/Nextcloud/Arbeit), damit "
                           "JARVIS die Termine des Nutzers kennt und in list_events berücksichtigt. Read-only.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "iCal-/ICS-URL (http/https/webcal)."},
                               "name": {"type": "string", "description": "Anzeigename (optional)."}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unsubscribe_calendar",
            "description": "Entfernt ein externes iCal-Abo (per Name).",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_calendar_subscriptions",
            "description": "Listet die externen iCal-Abos des Nutzers (Name, Termine, letzter Abgleich).",
            "parameters": {"type": "object", "properties": {}},
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
            "name": "send_image",
            "description": "Zeigt/sendet dem Nutzer eine im Workspace ERZEUGTE Bilddatei (z.B. ein mit matplotlib "
                           "erstelltes Diagramm): im Web-Chat wird das Bild angezeigt, per Telegram als Foto gesendet. "
                           "Rufe dies IMMER auf, nachdem du ein Bild gespeichert hast (z.B. savefig) — und behaupte "
                           "NIEMALS, ein Bild geschickt/gezeigt zu haben, ohne dieses Werkzeug tatsächlich aufzurufen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Dateipfad im Workspace, z.B. 'chart.png'."},
                    "caption": {"type": "string", "description": "Optionale Bildunterschrift."},
                },
                "required": ["path"],
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
            "name": "recall_conversation",
            "description": "Durchsucht FRÜHERE GESPRÄCHE mit diesem Nutzer (sitzungsübergreifend) nach relevanten "
                           "Wortwechseln. Nutze dies, wenn sich die Frage auf etwas zuvor Besprochenes bezieht "
                           "('was hatten wir neulich zu…', 'wie hieß nochmal das, worüber wir letzte Woche…').",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Worum ging es? (Suchbegriff/Thema)"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repair_skill",
            "description": "Repariert ein wiederholt scheiterndes Skill automatisch: analysiert den hinterlegten "
                           "letzten Fehler und korrigiert den Code (Selbst-Verbesserung). Nutze dies, wenn ein "
                           "Skill als instabil gemeldet ist.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Name des zu reparierenden Skills."}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_subagent",
            "description": "Delegiert eine abgegrenzte, MEHRSTUFIGE Teilaufgabe an einen Teil-Agenten, der sie "
                           "eigenständig mit Werkzeugen löst und nur das ENDERGEBNIS zurückgibt — hält deinen "
                           "Kontext klein. Ideal für umfangreiche Unteraufgaben (z.B. 'recherchiere gründlich X "
                           "und nenne 3 Kernpunkte'). Formuliere die Aufgabe klar und vollständig.",
            "parameters": {
                "type": "object",
                "properties": {"task": {"type": "string", "description": "Vollständig formulierte Teilaufgabe."}},
                "required": ["task"],
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
                           "ausgeführt und das Ergebnis genau an dieses Gerät gemeldet. "
                           "WICHTIG: Soll auf eine ÄNDERUNG/NEUE INHALTE einer Quelle gewartet werden "
                           "(„sag Bescheid, wenn…“), nutze stattdessen create_watch_automation (günstiger, zuverlässig).",
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
            "name": "create_watch_automation",
            "description": "Überwacht eine Quelle EFFIZIENT auf Änderungen und meldet nur bei einem echten Ereignis "
                           "(z.B. „behalte heise.de im Auge und sag Bescheid, wenn ein neuer Artikel erscheint“). Du "
                           "schreibst dafür ein kleines Python-PRÜFSKRIPT, das pro Intervall GÜNSTIG in der Sandbox läuft "
                           "(KEIN LLM pro Prüfung); erst wenn das Skript eine Änderung meldet, wird die eigentliche "
                           "Aufgabe ausgeführt. Nutze dieses Werkzeug — NICHT create_automation — immer dann, wenn auf "
                           "eine ÄNDERUNG oder NEUE INHALTE gewartet werden soll. Das Skript wird vor dem Speichern getestet.\n\n"
                           + watchers.SCRIPT_CONTRACT,
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Kurzer Name der Überwachung."},
                    "check_script": {"type": "string", "description": "Das Python-Prüfskript gemäß Vertrag "
                                     "(nutzt die vorgegebene Variable `state` und ruft `emit(...)` auf)."},
                    "task": {"type": "string", "description": "Was bei einer erkannten Änderung getan/gemeldet werden "
                             "soll, z.B. 'Informiere mich kurz über den neuen Artikel mit Titel und Link'."},
                    "interval_minutes": {"type": "integer", "description": "Prüfabstand in Minuten (Standard 15)."},
                },
                "required": ["title", "check_script", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_skill",
            "description": "Baut ein WIEDERVERWENDBARES Werkzeug (Skill) aus Python-Code, das danach jederzeit per "
                           "run_skill aufgerufen werden kann — ideal für wiederkehrende Aufgaben, die du effizienter "
                           "erledigen willst, statt jedes Mal neu zu überlegen. Der Code wird VOR dem Speichern getestet.\n\n"
                           + skills.SKILL_CONTRACT,
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Kurzer Name (Kleinbuchstaben/Unterstriche), z.B. 'paketstatus'."},
                    "description": {"type": "string", "description": "Was das Skill tut und wann man es nutzt."},
                    "code": {"type": "string", "description": "Python-Code gemäß Vertrag (liest `args`, ruft `result(...)`)."},
                    "params": {"type": "object", "description": "Beschreibung der Argumente, z.B. {\"stadt\": \"Stadtname\"}."},
                    "test_args": {"type": "object", "description": "Beispiel-Argumente zum Testlauf vor dem Speichern."},
                    "net": {"type": "boolean", "description": "true, wenn das Skill ins Internet darf (Standard false)."},
                    "pip": {"type": "array", "items": {"type": "string"},
                            "description": "Benötigte Python-Pakete (werden installiert)."},
                    "apt": {"type": "array", "items": {"type": "string"},
                            "description": "Benötigte System-Pakete wie 'nmap'. Nur in der erhöhten Spur installierbar; "
                                           "ein solches Skill braucht später Admin-Freigabe auf die Stufe Erhöht."},
                },
                "required": ["name", "description", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_skill",
            "description": "Führt ein vorhandenes selbst-gebautes Skill mit Argumenten aus und gibt dessen Ergebnis zurück.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name des Skills."},
                    "args": {"type": "object", "description": "Argumente für das Skill (passend zu seinen params)."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skills",
            "description": "Lädt die GETIPPTEN Werkzeuge der genannten Skills, sodass du sie danach direkt als "
                           "`skill__<name>` mit passenden Parametern aufrufen kannst (statt über run_skill). "
                           "Nutze dies, wenn du ein Skill mehrfach/typsicher verwenden willst.",
            "parameters": {
                "type": "object",
                "properties": {"names": {"type": "array", "items": {"type": "string"},
                                         "description": "Namen der zu ladenden Skills."}},
                "required": ["names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_skills",
            "description": "Sucht in den vorhandenen Skills (nach Name/Beschreibung). Leere Suche listet alle.",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string", "description": "Suchbegriff (optional)."}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_mcp_tools",
            "description": "Sucht externe MCP-Werkzeuge (z.B. Smart-Home) nach Stichwort. Liefert die vollen "
                           "Namen mcp__<server>__<tool>, die du danach mit load_mcp_tools laden kannst.",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string", "description": "Suchbegriff (optional)."}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_mcp_tools",
            "description": "Lädt die genannten externen MCP-Werkzeuge, sodass du sie danach direkt als "
                           "mcp__<server>__<tool> aufrufen kannst. Erst laden, dann aufrufen.",
            "parameters": {
                "type": "object",
                "properties": {"names": {"type": "array", "items": {"type": "string"},
                                         "description": "Volle Namen, z.B. mcp__domoticz__set_switch."}},
                "required": ["names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_skill",
            "description": "Zeigt Beschreibung, Parameter und Code eines Skills — vor dem Aufruf oder zum Anpassen.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_skill",
            "description": "Ändert ein vorhandenes Skill (Code/Beschreibung/Parameter/Netz/aktiviert). Code wird vor dem Speichern getestet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "code": {"type": "string"},
                    "params": {"type": "object"},
                    "test_args": {"type": "object", "description": "Beispiel-Argumente zum Testen geänderten Codes."},
                    "net": {"type": "boolean"},
                    "enabled": {"type": "boolean"},
                    "pip": {"type": "array", "items": {"type": "string"}, "description": "Python-Pakete."},
                    "apt": {"type": "array", "items": {"type": "string"}, "description": "System-Pakete (erhöhte Spur)."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_skill",
            "description": "Löscht ein selbst-gebautes Skill.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
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


# Werkzeuge, die für Entwicklung/Bau stehen → schalten den „Dev-Modus" der Session an (erzwingt Denken).
_DEV_TOOLS = {"create_skill", "update_skill", "delete_skill", "repair_skill", "run_python", "run_shell",
              "browse", "browser_click", "browser_type", "browser_screenshot", "create_watch_automation"}


# Tools, deren Ergebnis NICHT ins Tool-Gedächtnis soll (Steuer-/Timer-/Geschwätz-Tools).
_MEMORY_SKIP_TOOLS = {"set_timer", "list_timers", "cancel_timer", "describe_skill",
                      "load_skills", "load_mcp_tools", "send_message"}

# Fehler-Präfixe der Tool-Ergebnisse (deutsch) → für „ist der Aufruf geglückt?" (Verify-by-Tool #10).
_FAIL_PREFIXES = ("fehler", "berechtigung verweigert", "autonom gesperrt", "kein ", "keine ",
                  "unbekannt", "mir fehl", "abruf fehlgeschlagen", "der code ist nicht",
                  "das skill läuft", "mcp-aufruf an")


def _looks_failed(result: str) -> bool:
    r = (result or "").strip().lower()
    return any(r.startswith(p) for p in _FAIL_PREFIXES) or "fehlgeschlagen" in r[:60]


def _strip_code_fences(text: str) -> str:
    """Markdown-Codeblock-Zäune entfernen, falls das LLM doch welche liefert."""
    t = (text or "").strip()
    if t.startswith("```"):
        nl = t.find("\n")
        t = t[nl + 1:] if nl != -1 else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


async def repair_skill_impl(name: str, cfg: dict) -> str:
    """Selbst-Reparatur (Phase 2): hinterlegten Fehler + Code ans LLM, korrigierten Code syntaxgeprüft
    übernehmen. Code-Änderung setzt das Skill aus Sicherheitsgründen auf Sandbox/nicht-autonom zurück."""
    s = skills.get(name)
    if not s:
        return f"Kein Skill „{name}“ gefunden."
    err = s.get("last_error")
    if not err:
        return f"Skill „{s['name']}“ hat keinen hinterlegten Fehler — nichts zu reparieren."
    messages = [
        {"role": "system", "content":
            "Du bist ein erfahrener Python-Entwickler. Korrigiere das fehlerhafte Skill anhand der Fehlermeldung. "
            "Antworte AUSSCHLIESSLICH mit dem vollständigen, korrigierten Python-Code (mit einer Funktion "
            "run(args)) — ohne Erklärung, ohne Markdown-Zäune."},
        {"role": "user", "content":
            f"Skill „{s['name']}“. Letzter Fehler:\n{err}\n\nAktueller Code:\n{s.get('code', '')}"},
    ]
    try:
        res = await asyncio.to_thread(services.llm_call, messages, cfg, None, True)
    except Exception as e:
        return f"Reparatur fehlgeschlagen (LLM nicht erreichbar): {e}"
    code = _strip_code_fences(res.get("content", ""))
    if not code.strip():
        return "Die Reparatur lieferte keinen Code."
    ok, e = skills.syntax_ok(code)
    if not ok:
        return "Die reparierte Fassung ist syntaktisch ungültig: " + e
    skills.update(s["name"], code=code, trust="sandbox", autonomous_ok=False)
    skills.clear_health(s["name"])
    return (f"Skill „{s['name']}“ wurde repariert (Code aktualisiert, Syntax geprüft, Fehlerzähler zurückgesetzt; "
            "erhöhte Rechte zur Sicherheit zurückgenommen). Bitte einmal ausführen, um die Funktion zu bestätigen.")


_CALENDAR_TOOLS = {"add_event", "list_events", "update_event", "delete_event",
                   "share_calendar", "unshare_calendar", "calendar_subscription",
                   "subscribe_calendar", "unsubscribe_calendar", "list_calendar_subscriptions"}


def _cal_fmt(dt, all_day: bool = False) -> str:
    loc = dt.astimezone(calendars.LOCAL)
    return loc.strftime("%a %d.%m.%Y (ganztägig)") if all_day else loc.strftime("%a %d.%m.%Y %H:%M")


def _cal_bound(s: str | None, default):
    if not s:
        return default
    try:
        ss = s.strip()
        if len(ss) <= 10:
            d = date.fromisoformat(ss[:10])
            return datetime(d.year, d.month, d.day, tzinfo=calendars.LOCAL).astimezone(timezone.utc)
        return calendars.parse_dt(ss)
    except Exception:
        return default


def _calendar_dispatch(name: str, args: dict, ctx: dict, uid: int) -> str:
    """Synchroner Kalender-Handler (läuft im Thread; calendars.* ist DB-basiert/synchron)."""
    cfg = ctx.get("cfg") or config.get()
    if not cfg.get("calendar_enabled", True):
        return "Die Kalenderfunktion ist deaktiviert."
    is_admin = False
    try:
        is_admin = auth.is_admin(uid)
    except Exception:
        pass

    if name == "add_event":
        cal = calendars.resolve_calendar(uid, args.get("calendar"), ctx.get("username"))
        if not cal:
            return "Diesen Kalender finde ich nicht oder du hast keinen Zugriff."
        if calendars.access_level(uid, cal) not in ("write", "owner"):
            return f"Du hast nur Lesezugriff auf „{cal['name']}“."
        try:
            ev = calendars.add_event(cal["id"], uid, args["title"], args["start"], args.get("end"),
                                     args.get("description", ""), args.get("location", ""),
                                     bool(args.get("all_day")), args.get("recurrence", ""))
        except Exception as e:
            return f"Konnte den Termin nicht anlegen — prüfe das Zeitformat (ISO 8601, z. B. 2026-06-26T15:00): {e}"
        full = calendars.get_event(ev["id"])
        rec = f", Wiederholung {args.get('recurrence')}" if full["rrule"] else ""
        return f"Termin „{full['title']}“ am {_cal_fmt(full['start_ts'], full['all_day'])} in „{cal['name']}“ eingetragen (ID {ev['id']}{rec})."

    if name == "list_events":
        now = datetime.now(timezone.utc)
        start = _cal_bound(args.get("from"), now - timedelta(hours=1))
        end = _cal_bound(args.get("to"), now + timedelta(days=7))
        cal_id = None
        if args.get("calendar"):
            cal = calendars.resolve_calendar(uid, args.get("calendar"))
            if not cal:
                return "Diesen Kalender finde ich nicht."
            cal_id = cal["id"]
        evs = calendars.list_events(uid, start, end, cal_id)
        if not evs:
            return "In dem Zeitraum stehen keine Termine an."
        lines = []
        for e in evs:
            when = (e["start_ts"].astimezone(calendars.LOCAL).strftime("%a %d.%m. %H:%M") if not e["all_day"]
                    else e["start_ts"].astimezone(calendars.LOCAL).strftime("%a %d.%m. (ganztägig)"))
            loc = f" @ {e['location']}" if e["location"] else ""
            rec = " ↻" if e["rrule"] else ""
            lines.append(f"#{e['id']} [{e['calendar']}] {when} — {e['title']}{loc}{rec}")
        return "Termine:\n" + "\n".join(lines)

    if name == "update_event":
        ev = calendars.get_event(int(args.get("event_id") or 0))
        if not ev:
            return "Diesen Termin finde ich nicht."
        if not calendars.can_modify_event(uid, ev, is_admin):
            return "Diesen Termin darfst du nicht ändern (nur Ersteller, Kalenderbesitzer oder Admin)."
        fields = {k: args.get(k) for k in ("title", "start", "end", "description", "location", "all_day", "recurrence")}
        try:
            calendars.update_event(ev["id"], **fields)
        except Exception as e:
            return f"Konnte den Termin nicht ändern: {e}"
        return f"Termin #{ev['id']} aktualisiert."

    if name == "delete_event":
        ev = calendars.get_event(int(args.get("event_id") or 0))
        if not ev:
            return "Diesen Termin finde ich nicht (evtl. schon gelöscht)."
        if not calendars.can_modify_event(uid, ev, is_admin):
            return "Diesen Termin darfst du nicht löschen (nur Ersteller, Kalenderbesitzer oder Admin)."
        calendars.delete_event(ev["id"])
        return f"Termin „{ev['title']}“ wurde ausgetragen."

    if name == "share_calendar":
        u = auth.user_by_name((args.get("username") or "").strip())
        if not u:
            return f"Nutzer „{args.get('username')}“ nicht gefunden."
        if u["id"] == uid:
            return "Das ist dein eigener Kalender."
        calendars.share(uid, u["id"], args.get("access", "read"))
        acc = "schreiben" if str(args.get("access", "")).lower().startswith("w") else "lesen"
        return f"Dein Kalender ist jetzt für {u['username']} freigegeben ({acc})."

    if name == "unshare_calendar":
        u = auth.user_by_name((args.get("username") or "").strip())
        if not u:
            return f"Nutzer „{args.get('username')}“ nicht gefunden."
        calendars.unshare(uid, u["id"])
        return f"Freigabe für {u['username']} entzogen."

    if name == "calendar_subscription":
        base = (cfg.get("calendar_base_url") or "").rstrip("/")
        utok = calendars.user_token(uid)
        cals = calendars.list_accessible(uid)
        lines = [f"Kombinierter Abo-Link (alle deine Kalender):\n{base}/calendar/user/{utok}.ics", "",
                 "Einzelne Kalender:"]
        for c in cals:
            lines.append(f"- {c['name']} ({c['access']}): {base}/calendar/cal/{c['ics_token']}.ics")
        lines.append("\nIn der Kalender-App als Abo-/Kalender-URL hinzufügen — nur im LAN erreichbar "
                     "(Zertifikat selbstsigniert).")
        return "\n".join(lines)

    if name == "subscribe_calendar":
        url = (args.get("url") or "").strip().replace("webcal://", "https://")
        if not url.startswith(("http://", "https://")):
            return "Bitte eine gültige iCal-URL (http/https/webcal) angeben."
        res = calendars.add_subscription(uid, args.get("name") or "Externer Kalender", url)
        if res.get("error"):
            return f"Abonniert, aber der erste Abgleich schlug fehl: {res['error']}"
        return f"Kalender „{res['name']}“ abonniert — {res.get('synced', 0)} Termine übernommen. JARVIS kennt sie jetzt."

    if name == "unsubscribe_calendar":
        ok = calendars.remove_subscription(uid, (args.get("name") or "").strip())
        return "Abo entfernt." if ok else "Kein passendes Abo gefunden."

    if name == "list_calendar_subscriptions":
        subs = calendars.list_subscriptions(uid)
        if not subs:
            return "Du hast keine externen Kalender abonniert."
        lines = []
        for s in subs:
            err = f" ⚠ {s['last_error']}" if s["last_error"] else ""
            lines.append(f"- {s['name']}: {s['events']} Termine (Stand {s['last_sync'] or 'nie'}){err}")
        return "Abonnierte Kalender:\n" + "\n".join(lines)

    return "Unbekannte Kalenderaktion."


_TODO_TOOLS = {"add_todo", "list_todos", "complete_todo", "remove_todo", "todo_link"}


def _todo_dispatch(name: str, args: dict, ctx: dict, uid: int) -> str:
    """Synchroner To-do-Handler (läuft im Thread; todos.* ist DB-basiert)."""
    cfg = ctx.get("cfg") or config.get()
    if name == "add_todo":
        text = (args.get("text") or "").strip()
        if not text:
            return "Was genau soll auf die To-do-Liste?"
        t = todos.add(uid, text, (args.get("due") or "").strip() or None)
        extra = f" (fällig {t['due_date']} — steht auch im Kalender)" if t["due_date"] else ""
        return f"„{t['text']}“ steht jetzt auf deiner To-do-Liste{extra}."

    if name == "list_todos":
        scope = (args.get("scope") or "open").lower()
        items = todos.list_todos(uid, include_done=(scope in ("all", "done")))
        if scope == "done":
            items = [t for t in items if t["done"]]
        elif scope == "open":
            items = [t for t in items if not t["done"]]
        if not items:
            return "Deine To-do-Liste ist leer."
        lines = []
        for t in items:
            box = "✓" if t["done"] else "•"
            due = f" (bis {t['due_date']})" if t["due_date"] else ""
            lines.append(f"{box} {t['text']}{due}")
        return "Deine To-do-Liste:\n" + "\n".join(lines)

    if name == "complete_todo":
        t = todos.match(uid, args.get("item") or "", (args.get("due") or "").strip() or None)
        if not t:
            return f"Ich finde „{args.get('item')}“ nicht auf deiner offenen To-do-Liste."
        todos.set_done(t["id"], True)
        return f"Erledigt — „{t['text']}“ abgehakt."

    if name == "remove_todo":
        t = todos.match(uid, args.get("item") or "", (args.get("due") or "").strip() or None)
        if not t:
            return f"Ich finde „{args.get('item')}“ nicht auf deiner To-do-Liste."
        todos.remove(t["id"])
        return f"„{t['text']}“ von der To-do-Liste entfernt."

    if name == "todo_link":
        base = (cfg.get("calendar_base_url") or "").rstrip("/")
        return (f"Deine To-do-Liste (smartphone-optimiert, ohne Login abhakbar):\n"
                f"{base}/todo/{todos.share_token(uid)}")

    return "Unbekannte To-do-Aktion."


async def execute_tool(name: str, args: dict, ctx: dict) -> str:
    """Wrapper mit Debug-Aufzeichnung (Tool-Name, Argumente, Ergebnis, Dauer)."""
    t0 = time.time()
    cb = ctx.get("status_cb")            # #11 Fortschritt: welches Werkzeug läuft gerade (nicht-streamender Pfad)
    if cb:
        try:
            cb(name)
        except Exception:
            pass
    if name in _DEV_TOOLS:
        hub.mark_dev(ctx.get("session_id"))
    result = await _execute_tool_impl(name, args, ctx)
    debug.log("tool", name=name, args=args, result=str(result)[:400],
              ms=int((time.time() - t0) * 1000), user_id=ctx.get("user_id"))
    # Vollständiger Mitschnitt ALLER Tool-Aufrufe dieses Turns (für Verify-by-Tool #10) — inkl. Erfolg.
    ctx.setdefault("turn_tool_calls", []).append({"name": name, "ok": not _looks_failed(str(result))})
    # Tool-Ergebnis-Gedächtnis: substanzielle Ergebnisse knapp mitschneiden, damit der Folge-Turn
    # Rückfragen zum zuvor Geholten beantworten kann (Meta-/Steuer-Tools überspringen).
    if name not in _MEMORY_SKIP_TOOLS and not name.startswith(("load_", "search_", "list_")):
        ctx.setdefault("turn_tools", []).append({"name": name, "result": str(result)[:1500]})
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

    if name == "create_watch_automation":
        return await _create_watch_automation(args, ctx, sid)

    # ── Selbst-gebaute Skills ─────────────────────────────────────────────────
    if name == "create_skill":
        sname = skills.sanitize_name(args.get("name") or "")
        code = (args.get("code") or "").strip()
        if not sname or not code:
            return "Mir fehlen Name oder Code für das Skill."
        apt, pip = args.get("apt") or [], args.get("pip") or []
        if pip:                                               # Python-Pakete sofort installieren (Sandbox-Spur)
            await asyncio.to_thread(sandbox.install, [], pip, False)
        if args.get("test_args") is not None:                 # mit Beispiel-Args → echter Funktionstest
            test = await asyncio.to_thread(skills.run_skill_code, code, args["test_args"], ns, bool(args.get("net")))
            if not test["ok"] and not (apt or pip):
                return ("Das Skill läuft noch nicht sauber — korrigiere den Code und rufe create_skill erneut auf:\n"
                        + test["error"])
        else:                                                 # ohne Args → nur Syntax prüfen
            ok, err = skills.syntax_ok(code)
            if not ok:
                return "Der Code ist nicht gültig: " + err
            test = {"ok": True, "result": "(nur Syntax geprüft — ohne test_args kein Funktionslauf)"}
        s = skills.create(sname, args.get("description") or "", code, params=args.get("params") or {},
                          owner_user_id=ctx.get("user_id"), net=bool(args.get("net")), apt=apt, pip=pip)
        extra = ""
        if pip:
            extra += f" Python-Pakete installiert: {', '.join(pip)}."
        if apt:
            extra += (f" System-Pakete ({', '.join(apt)}) brauchen die erhöhte Spur — bitte im Admin-UI auf die Stufe "
                      "Erhöht freischalten, dann installiere ich sie automatisch.")
            if not test.get("ok"):
                extra += " (Voller Test folgt nach der Freischaltung.)"
        return (f"Skill „{s['name']}“ (v{s['version']}) angelegt. Aufruf via run_skill name=\"{s['name']}\".{extra} "
                f"Testergebnis: {test.get('result') or test.get('error')}")

    if name == "run_skill":
        s = skills.get(args.get("name") or "")
        if not s or not s.get("enabled"):
            return f"Kein aktives Skill „{args.get('name')}“. Mit search_skills suchen oder create_skill bauen."
        return await _exec_skill(s, args.get("args") or {}, ns, ctx)

    if name.startswith("skill__"):                       # getipptes Skill (per load_skills geladen)
        s = skills.get(name[len("skill__"):])
        if not s or not s.get("enabled"):
            return f"Skill „{name}“ ist nicht verfügbar."
        return await _exec_skill(s, args, ns, ctx)

    if name == "load_skills":
        loaded = ctx.setdefault("loaded_skills", set())
        if not isinstance(loaded, set):
            loaded = set(loaded); ctx["loaded_skills"] = loaded
        found = []
        for n in args.get("names") or []:
            s = skills.get(n)
            if s and s.get("enabled"):
                loaded.add(s["name"]); found.append(s["name"])
        if not found:
            return "Keine passenden aktiven Skills gefunden."
        return "Geladen — direkt aufrufbar: " + ", ".join(f"skill__{n}" for n in found)

    if name == "search_skills":
        found = skills.search(args.get("query") or "")
        if not found:
            return "Keine passenden Skills vorhanden. Du kannst mit create_skill ein neues bauen."
        return "Skills:\n" + "\n".join(f"- {s['name']}: {s['description']}" for s in found)

    if name == "search_mcp_tools":
        found = mcp_hub.search(args.get("query") or "")
        if not found:
            return "Keine passenden externen MCP-Werkzeuge gefunden."
        return "MCP-Werkzeuge:\n" + "\n".join(f"- {t['full_name']}: {t['description']}" for t in found)

    if name == "load_mcp_tools":
        loaded = ctx.setdefault("loaded_mcp", set())
        if not isinstance(loaded, set):
            loaded = set(loaded); ctx["loaded_mcp"] = loaded
        found = []
        for n in args.get("names") or []:
            if mcp_hub._schema_for_full(n):
                loaded.add(n); found.append(n)
        if not found:
            return ("Keine dieser MCP-Werkzeuge gefunden. Mit search_mcp_tools die korrekten "
                    "Namen mcp__<server>__<tool> ermitteln.")
        return "Geladen — jetzt direkt aufrufbar: " + ", ".join(found)

    if name == "describe_skill":
        s = skills.get(args.get("name") or "")
        if not s:
            rest = [x["name"] for x in skills.list_all()]
            return ("Es gibt kein Skill mit diesem Namen (evtl. gelöscht). "
                    + ("Vorhandene Skills: " + ", ".join(rest) if rest else "Es gibt aktuell keine Skills."))
        return (f"Skill {s['name']} (v{s.get('version', 1)}, Läufe {s.get('run_count', 0)}, "
                f"Netz {s.get('net')}):\n{s['description']}\nParameter: {s.get('params') or {}}\nCode:\n{s['code']}")

    if name == "update_skill":
        s = skills.get(args.get("name") or "")
        if not s:
            rest = [x["name"] for x in skills.list_all()]
            return ("Es gibt kein Skill mit diesem Namen zum Ändern (evtl. gelöscht). "
                    + ("Vorhandene Skills: " + ", ".join(rest) + ". " if rest else "Es gibt aktuell keine Skills. ")
                    + "Lege es bei Bedarf mit create_skill neu an.")
        has_deps = bool(args.get("apt") or args.get("pip") or s.get("apt") or s.get("pip"))
        if args.get("code"):
            if args.get("test_args") is not None:             # mit Beispiel-Args → echter Funktionstest
                test = await asyncio.to_thread(skills.run_skill_code, args["code"], args["test_args"],
                                               ns, bool(args.get("net", s.get("net"))))
                if not test["ok"] and not has_deps:
                    return "Der geänderte Code läuft nicht sauber:\n" + test["error"]
            else:                                             # ohne Args → nur Syntax prüfen
                ok, err = skills.syntax_ok(args["code"])
                if not ok:
                    return "Der geänderte Code ist nicht gültig: " + err
        fields = {k: args[k] for k in ("description", "code", "params", "net", "enabled", "apt", "pip") if k in args}
        if "code" in fields:                              # geänderter Code → erhöhte Rechte zurücksetzen (Re-Review)
            fields["trust"] = "sandbox"; fields["autonomous_ok"] = False
        skills.update(s["name"], **fields)
        return f"Skill „{s['name']}“ aktualisiert."

    if name == "delete_skill":
        wanted = args.get("name") or ""
        ok = skills.delete(wanted)
        rest = [s["name"] for s in skills.list_all()]
        rest_line = ("Aktuell vorhandene Skills: " + ", ".join(rest)) if rest else "Es gibt jetzt keine Skills mehr."
        if ok:
            return (f"Skill „{skills.sanitize_name(wanted)}“ wurde gelöscht. {rest_line} "
                    "Die Löschung ist erledigt — bestätige sie dem Nutzer und rufe delete_skill NICHT erneut auf.")
        # Schon weg / nie da: idempotent klar machen, damit das Modell nicht erneut löscht oder das Gegenteil behauptet.
        return (f"Es gibt kein Skill namens „{skills.sanitize_name(wanted)}“ — es ist bereits gelöscht bzw. existiert "
                f"nicht. {rest_line} Behandle das als erledigt; behaupte NICHT, das Skill sei noch vorhanden.")

    if name == "repair_skill":
        return await repair_skill_impl(args.get("name") or "", ctx.get("cfg") or config.get())

    if name == "spawn_subagent":
        import subagent
        if not subagent.can_spawn(ctx):
            return "Verschachtelte Teil-Agenten sind nicht erlaubt — erledige die Aufgabe direkt."
        task = (args.get("task") or "").strip()
        if not task:
            return "Fehler: keine Aufgabe für den Teil-Agenten angegeben."
        return await subagent.run_subagent(task, ctx)

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

    if name == "get_datetime":               # Tool entfernt; Zeit steht im Prompt. Fallback, falls Altverlauf.
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

    if name == "save_note":
        if not ctx.get("username"):
            return ("Ich weiß nicht, in wessen Obsidian-Vault ich speichern soll — dafür müsstest du als "
                    "erkannter Nutzer angemeldet sein (Stimme/Telegram/Login).")
        ok, msg = await asyncio.to_thread(obsidian.save_note, ctx.get("username"),
                                          args.get("text") or "", args.get("title"), ctx["cfg"])
        return msg

    if name in _CALENDAR_TOOLS:
        uid = ctx.get("user_id")
        if not uid:
            return ("Kalender sind pro Nutzer — dafür müsstest du als erkannter Nutzer angemeldet sein "
                    "(Stimme/Telegram/Login).")
        return await asyncio.to_thread(_calendar_dispatch, name, args, ctx, uid)

    if name in _TODO_TOOLS:
        uid = ctx.get("user_id")
        if not uid:
            return ("To-do-Listen sind pro Nutzer — dafür müsstest du als erkannter Nutzer angemeldet sein "
                    "(Stimme/Telegram/Login).")
        return await asyncio.to_thread(_todo_dispatch, name, args, ctx, uid)

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

    if name == "send_image":
        path = (args.get("path") or "").strip()
        caption = (args.get("caption") or "").strip()
        if not path:
            return "Mir fehlt der Dateipfad des Bildes."
        data = await asyncio.to_thread(sandbox.read_bytes, ns, path)
        if not data.get("ok"):
            return (f"Bilddatei „{path}“ nicht lesbar: {data.get('error', 'unbekannt')}. "
                    "Hast du sie wirklich im Workspace gespeichert (z.B. mit savefig)?")
        fname = data.get("name") or "bild.png"
        low = fname.lower()
        mime = "image/png" if low.endswith(".png") else ("image/jpeg" if low.endswith((".jpg", ".jpeg"))
               else "image/gif" if low.endswith(".gif") else "application/octet-stream")
        channel = ctx.get("channel") or "browser"
        if channel == "telegram":
            uid = ctx.get("user_id")
            if uid is None:
                return "Ich erkenne dich gerade nicht — ich weiß nicht, an welchen Telegram-Chat das Bild soll."
            raw = base64.b64decode(data["b64"])
            ok = await asyncio.to_thread(messaging.send_photo_to_user, uid, raw, caption, fname)
            return "Bild per Telegram gesendet." if ok else \
                   "Telegram-Foto-Versand fehlgeschlagen (verifizierte Chat-ID hinterlegt?)."
        if channel == "satellite":
            return "Ich habe das Bild erstellt, kann es auf einem reinen Sprachgerät aber nicht anzeigen."
        # Web-Chat → Bild als Data-URI über /ws an die Session pushen
        delivered = await hub.push(sid, {"type": "attachment", "mime": mime, "name": fname, "caption": caption,
                                         "data_uri": f"data:{mime};base64,{data['b64']}"})
        return "Bild wird im Chat angezeigt." if delivered else \
               "Konnte das Bild nicht ausliefern (keine aktive Web-Verbindung zu diesem Chat)."

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

    if name == "recall_conversation":
        query = (args.get("query") or "").strip()
        if not query:
            return "Fehler: Keine Suchanfrage."
        if ns == "guest":
            return "Für nicht erkannte Gäste gibt es kein sitzungsübergreifendes Gesprächsgedächtnis."
        try:
            hits = await asyncio.to_thread(knowledge.recall_conversation, ctx["cfg"], query, ns)
        except Exception as e:
            return f"Gesprächs-Recall fehlgeschlagen: {e}"
        if not hits:
            return "Dazu finde ich kein früheres Gespräch."
        return "Frühere Gespräche dazu:\n" + "\n---\n".join(
            f"(Relevanz {h['score']:.2f}) {h['content']}" for h in hits)

    return f"Unbekanntes Tool: {name}"


def _url_is_safe(url: str) -> bool:
    """SSRF-Schutz: nur http(s). Private LAN-Adressen nur mit Admin-Freigabe (fetch_allow_lan);
    Loopback/Link-Local/Reserved/Multicast bleiben IMMER gesperrt."""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    allow_lan = bool(config.get().get("fetch_allow_lan", False))
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        for fam, _, _, _, sockaddr in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(sockaddr[0])
            if ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
            if ip.is_private and not allow_lan:
                return False
        return True
    except Exception:
        return False


def _fetch_url(url: str, max_chars: int = 4000) -> str:
    """Webseite laden und Titel/Überschriften/Lesetext extrahieren (lxml)."""
    if not _url_is_safe(url):
        if not config.get().get("fetch_allow_lan", False):
            return ("Diese URL zeigt auf eine interne/lokale Adresse — standardmäßig aus Sicherheitsgründen gesperrt. "
                    "Der Admin kann LAN-Zugriff im Admin-UI (System → Netzwerkzugriff) aktivieren; dann klappt es.")
        return "Diese URL ist nicht erlaubt (nur http/https; Loopback/Link-Local bleiben gesperrt)."
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
    # Quelltext/JSON/Plaintext (kein HTML/XML) → ROH zurückgeben, aber kontext-sicher kappen.
    # ACHTUNG: zu große Ergebnisse sprengen das Kontextfenster des LLM (llama.cpp wirft dann
    # HTTP 400 exceed_context_size_error → Turn endet ohne Antwort). Default ~20k Zeichen
    # (~7k Tokens) lässt genug Platz für System-Prompt, Tool-Schemas und Verlauf.
    if "html" not in ctype and "xml" not in ctype:
        LIM = int(config.get().get("fetch_max_chars", 20000))
        if len(html) > LIM:
            return (f"Quelle: {url} ({ctype})\n\n" + html[:LIM]
                    + f"\n\n…[GEKÜRZT — nur die ersten {LIM} von {len(html)} Zeichen. "
                      "Die Datei ist zu groß fürs Kontextfenster. Beantworte die Frage mit dem "
                      "sichtbaren Teil; falls das Gesuchte fehlt, sag das offen — rate nicht.]")
        return f"Quelle: {url} ({ctype})\n\n" + html
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


async def _create_watch_automation(args: dict, ctx: dict, sid: str) -> str:
    """Watcher anlegen: Prüfskript zuerst in der Sandbox testen (Baseline merken), dann speichern."""
    title = (args.get("title") or "").strip()
    task = (args.get("task") or "").strip()
    script = (args.get("check_script") or "").strip()
    if not script:
        return "Mir fehlt das Prüfskript (check_script)."
    try:
        mins = int(args.get("interval_minutes") or 15)
    except (TypeError, ValueError):
        mins = 15
    if mins <= 0:
        mins = 15
    uid = ctx.get("user_id")
    namespace = f"u{uid}" if uid else "guest"
    # Test-Lauf mit leerem Zustand: fängt Skript-Fehler VOR dem Speichern ab und etabliert die
    # Baseline (aktueller Stand), damit beim ersten echten Tick nicht alles als „neu“ gemeldet wird.
    test = await asyncio.to_thread(watchers.run_check, {"check_script": script, "state": {}, "net": True}, namespace)
    if not test.get("ok"):
        return ("Das Prüfskript läuft noch nicht sauber — bitte korrigiere es anhand des Fehlers und rufe "
                "create_watch_automation erneut auf:\n" + test.get("error", ""))
    baseline = test["parsed"].get("state") if isinstance(test["parsed"].get("state"), dict) else {}
    a = automations.manager.create(
        title=title, task=task, trigger={"type": "interval", "seconds": mins * 60},
        owner_user_id=uid, target_session=sid, kind="watcher", check_script=script, net=True)
    automations.manager.update(a["id"], state=baseline)
    return (f"Überwachung „{a['title']}“ eingerichtet — ich prüfe alle {mins} min günstig per Skript "
            "und melde mich erst, wenn sich wirklich etwas ändert.")


async def _exec_skill(s: dict, sargs: dict, ns: str, ctx: dict) -> str:
    """Führt ein Skill aus, zählt Erfolg/Fehler und gibt bei Fehler einen Reparatur-Hinweis (Self-Heal-Nudge)."""
    trust = s.get("trust", "sandbox")
    # Erhöhte Skills (Hostnetz/Raw) NIE autonom, außer der Admin hat es für genau dieses Skill erlaubt.
    if trust == "elevated" and ctx.get("autonomous") and not s.get("autonomous_ok"):
        return (f"Skill „{s['name']}“ hat erhöhte Rechte und darf NICHT autonom laufen — nur in einem "
                "interaktiven Gespräch.")
    r = await asyncio.to_thread(skills.run_skill_code, s["code"], sargs or {}, ns, s.get("net", False), trust)
    skills.record_run(s["name"], r["ok"], r.get("error"))
    if not r["ok"]:
        return (f"Skill „{s['name']}“ Fehler: {r['error']}\n"
                "Du kannst das Skill mit update_skill korrigieren (gib den verbesserten Code an).")
    res_str = str(r["result"])
    if len(res_str) > 6000:
        res_str = (res_str[:6000] + f" …[gekürzt — {len(res_str)} Zeichen gesamt; filtere/aggregiere große "
                   "Ergebnisse besser direkt im Skill, statt alles roh zurückzugeben]")
    return f"Ergebnis von {s['name']}: {res_str}"


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
