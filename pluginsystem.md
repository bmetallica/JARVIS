# JARVIS Pluginsystem — Architektur, Schnittstellen & Einsatz

**Status:** ✅ **implementiert & getestet (v1, Phase 0+1+2)** · **Datum:** 2026-06-25 · **Sprache:** Deutsch (Pflicht)

> **Einsatzbereit.** Das Gateway `/api/v1/*`, der Plugin-API-Key-Mechanismus, die Plugin-Registry,
> der KV-/RAG-Store, der Event-WebSocket, die Scheduler-Fassade und die Admin-Endpunkte sind im
> Orchestrator implementiert und laufen live auf `:8088`. Schnelleinstieg → **Kapitel 14**.
> Ein lauffähiges Plugin-Template liegt unter `deploy/plugin-example/`.

Dieses Dokument beschreibt ein **entkoppeltes Pluginsystem** für JARVIS. Ziel ist, dass
externe Anwendungen (z. B. der geplante **ADHS-Helper** als PWA) sämtliche JARVIS-Subsysteme
— LLM, Vision, STT/TTS, RAG/Gedächtnis, Telegram, Satelliten, Tools, Event-Bus, Scheduler —
über eine **stabile, versionierte, authentifizierte API** mitnutzen können, ohne fest in den
Core verdrahtet zu sein.

Das Dokument ist so gehalten, dass **ein separates Team den ADHS-Helper allein anhand dieser
Doku** entwickeln könnte (vollständige Endpunkt-Referenz + Plugin-Template + Voraussetzungen in
Kapitel 8, 9 und Anhang A/B).

---

## 0. Bestandsaufnahme: Worauf wir aufsetzen

Das Pluginsystem ist **kein Neubau**, sondern eine **Fassade (API-Gateway)** vor bereits
existierenden Modulen des Orchestrators (`/opt/JARVIS/orchestrator`, FastAPI, Port 8088).
Die folgende Tabelle zeigt, was schon da ist und nur exponiert werden muss, und was neu gebaut wird.

| Fähigkeit | Vorhandenes Modul / Funktion | Status |
|---|---|---|
| LLM Chat / Stream | `services.chat`, `services.llm_call`, `services.llm_stream` (SSE-Satzstrom) | ✅ vorhanden |
| LLM + Tools (Agenten-Loop) | `app._run_loop`, `tools.execute_tool`, `tools.TOOL_SCHEMAS` | ✅ vorhanden |
| Vision | `services.vision_call`, Endpoint `/api/vision`, Tool `analyze_image` | ✅ vorhanden |
| STT | `services.transcribe`, Endpoint `/api/stt` | ✅ vorhanden |
| TTS | `services.synthesize`, Endpoint `/api/tts` | ✅ vorhanden |
| Embeddings | `services.embed` (nomic-embed-text) | ✅ vorhanden |
| RAG / Gedächtnis | `knowledge.py` (+ `store.py`, pgvector :5440); kinds `memory`/`conversation`/`document`, namespace `u<uid>` | ✅ vorhanden |
| Universeller Rückkanal | `app.announce(session_id, text, kind, **meta)` → Browser/Pi (JSON), ESP (PCM/TTS) | ✅ vorhanden |
| Telegram | `messaging.send_to_user`, `messaging.send_photo_to_user` | ✅ vorhanden |
| Event-Bus | `automations.emit(event, payload)` + `automations.manager` (geplant + ereignisgesteuert) | ✅ vorhanden |
| Scheduler / Autonomie | `automations.manager` (Cron + Events), Runner `app._run_automation` | ✅ vorhanden |
| Geräte/Sessions | `session_hub.hub` (Sessions, Push, Capabilities, Render-Mode) | ✅ vorhanden |
| Nutzer/Gruppen/Rechte | `auth.py` — `group_permissions`, Ressourcen `tool:*`, `mcp:*`, `*`; `auth.is_tool_allowed` | ✅ vorhanden |
| Externe Tools (Vorbild) | `mcp_hub.py` — externe MCP-Server als Tools, im Admin-UI verwaltet | ✅ vorhanden (Vorbild für Plugin-Registry) |
| **Plugin-API-Keys / Bearer-Auth** | `plugins_registry.py` (verify_key, Scopes) | ✅ implementiert (Kap. 4) |
| **`/api/v1/*` Gateway-Router** | `api_v1.py` (FastAPI APIRouter, 28 Routen) | ✅ implementiert (Kap. 5) |
| **Plugin-Registry + Manifest** | `plugins_registry.py` + Admin-Endpunkte in `app.py` | ✅ implementiert (Kap. 7) |
| **Plugin-Event-WebSocket** | `api_v1.py:/api/v1/ws` + `plugin_bus.py` | ✅ implementiert (Kap. 6) |
| **Plugin-Storage (KV/Doc)** | `plugins_registry.kv_*` (Tabelle `plugin_kv`) | ✅ implementiert (Kap. 5.7) |
| **Scheduler-Fassade** | `app._plugin_schedule_job` über `automations.manager` | ✅ implementiert (Kap. 5.9) |
| **Core-Event-Bridge** | `automations.add_listener` → `plugin_bus.forward_core_event` | ✅ implementiert (Kap. 6) |
| **CORS für Browser-PWAs** | `app._cors_gateway` (dynamisch: config + Plugin-`ui.entry`) | ✅ implementiert (Kap. 14.4) |
| **Admin-UI-Tab „🧩 Plugins"** | `static/admin.html` + `admin.js` | ✅ implementiert (Kap. 7/14.2) |
| **Server-Plugin-Loader** | analog `skills.py`/`mcp_hub.py` | ⬜ geplant, optional (Kap. 9) |

> **Wichtige Designkonsequenz:** Die heutige Authentifizierung kennt nur (a) Admin-Cookie
> `jarvis_admin_token` und (b) Nutzer-Identität pro `session_id` (Stimme oder Passwort-Login).
> Für maschinelle Plugin-Clients fehlt ein **API-Key-Mechanismus** — das ist das zentrale neue
> Stück Infrastruktur.

---

## 1. Designprinzipien

1. **API-First & Entkopplung.** JARVIS ist das „Betriebssystem des Hauses“ und stellt
   Infrastruktur bereit. Plugins sind eigenständige Clients/Module. Der Core bleibt schlank.
2. **Versioniert & stabil.** Alle Plugin-Schnittstellen liegen unter `/api/v1/…`. Breaking
   Changes nur über `/api/v2/…`. Interne Modul-Signaturen dürfen sich ändern, die `v1`-Fassade nicht.
3. **Rechte wiederverwenden, nicht neu erfinden.** Plugin-Scopes bilden auf das bestehende
   `group_permissions`-Ressourcenmodell ab (neue Präfixe `plugin:<id>` und `api:<capability>`).
4. **Mandantentrennung (Multi-Tenancy).** Jedes Plugin bekommt einen festen **Namespace**
   (`plugin:<id>`). RAG-Collections, KV-Store und Events sind je Plugin isoliert.
5. **Nutzer-Identität durchreichen.** Ein Plugin handelt **im Auftrag eines JARVIS-Nutzers**.
   Datenisolierung erfolgt zusätzlich pro Nutzer (`u<uid>`), damit „Vater“ und „Mutter“ im
   ADHS-Helper getrennte RAG-Logs haben.
6. **Defense in Depth.** Auch wenn das Gateway einen Aufruf zulässt, prüfen die Zielmodule
   (`auth.is_tool_allowed`, Autonomie-Blacklist) erneut.
7. **Zwei Integrationstiefen** (siehe Kapitel 3): **extern** (reiner HTTP/WS-Client) und
   **intern** (Server-Modul, das im Orchestrator mitläuft) — frei kombinierbar.
8. **Kein Doppel-TTS, kanal-bewusst.** Ausgaben laufen über `announce()`, das selbst
   entscheidet, ob ein Gerät TTS lokal rendert (Browser/Pi) oder serverseitig PCM braucht (ESP).
9. **Deutsch durchgängig.** System-Prompts, TTS-Stimmen, STT-Sprache default `de`.

---

## 2. Architekturüberblick

```
                          ┌──────────────────────────────────────────────┐
   EXTERNE PLUGINS        │                 JARVIS CORE                   │
 (eigene Prozesse/Apps)   │            (Orchestrator, :8088)              │
                          │                                              │
 ┌───────────────┐  REST  │  ┌────────────────────────────────────────┐ │
 │ ADHS-Helper   │◄──────►│  │  API-GATEWAY  /api/v1/*   (NEU)         │ │
 │ (PWA, Phone)  │  WS    │  │  + Plugin-Auth (Bearer API-Key)        │ │
 └───────────────┘◄──────►│  │  + Scope-Check  + Rate-Limit           │ │
                          │  └───────────────┬────────────────────────┘ │
 ┌───────────────┐        │                  │ delegiert intern an:      │
 │ Vision-Plugin │◄──────►│   ┌──────────────┼───────────────────────┐  │
 │ (Kamera/Cam)  │        │   ▼      ▼        ▼         ▼        ▼     │  │
 └───────────────┘        │ services  knowledge  announce  messaging   │  │
                          │ (LLM/STT/  (RAG/    (Rückkanal)(Telegram)  │  │
 ┌───────────────┐        │  TTS/Vis)  pgvector) Satelliten            │  │
 │ INTERNES      │ Python │   ▲                  ▲          ▲           │  │
 │ Server-Plugin │◄──────►│   │  automations.emit / manager (Events,   │  │
 │ (plugins/…)   │  Hooks │   │  Scheduler)   session_hub (Geräte)     │  │
 └───────────────┘        │   └────────────────────────────────────────┘  │
                          │                                              │
                          │  Plugin-Registry (NEU, analog mcp_hub)       │
                          │  Plugin-KV/Doc-Store (NEU, store.py)         │
                          └──────────────────────────────────────────────┘
```

**Zwei Kommunikationskanäle** (wie im Material vorgeschlagen):
- **REST** (`/api/v1/…`) für synchrone Anfragen (Chat, JSON-Strukturierung, RAG-Query, Notify).
- **WebSocket-Event-Bus** (`/api/v1/ws`) für asynchrone Echtzeit (Timer-Ticks, Gamification-XP,
  Geräte-Events, eingehende Sprachbefehle, plattformübergreifende Synchronisation).

---

## 3. Plugin-Typen

### Typ A — Externes Plugin (Client) · *Standardfall für den ADHS-Helper*
Eine eigenständige App (PWA, Desktop, Microservice) außerhalb des Orchestrator-Prozesses.
Kommuniziert ausschließlich über `/api/v1/*` (REST) und `/api/v1/ws` (Events). Authentifiziert
sich mit einem **Plugin-API-Key** (Bearer-Token). Hält bei Bedarf eigene Daten — oder nutzt den
**Plugin-KV/Doc-Store** und die **RAG-Collections** von JARVIS, um keine eigene DB zu brauchen.

> Der ADHS-Helper läuft als reine PWA; Tamagotchi-Zustand, Tasks und XP liegen entweder in der
> PWA (IndexedDB) **oder** im Plugin-KV-Store von JARVIS (empfohlen, damit alle Geräte/Familien-
> mitglieder synchron sind). Gamification-Ticks und Nudges laufen über Events + Scheduler.

### Typ B — Internes Plugin (Server-Modul) · *optional, für serverseitige Logik*
Ein Python-Paket unter `orchestrator/plugins/<id>/`, das beim Start geladen wird und über
**Hooks** eigene Tools, Routen, Event-Handler und geplante Jobs registriert (Kapitel 9).
Sinnvoll, wenn ein Plugin **dauerhaft serverseitige Logik** braucht (z. B. die ADHS-Gamification-
Engine, die XP berechnet und nächtliche „sanfte Neustarts“ plant), oder neue **Tools für den
Sprachassistenten** beisteuern will („Jarvis, wie geht es dem Familien-Tamagotchi?“).

**Beide Typen teilen sich dieselbe Registry, denselben Namespace und dieselben Scopes.** Ein
Plugin kann rein extern, rein intern oder **hybrid** sein (Server-Modul für Logik + PWA fürs UI).

---

## 4. Authentifizierung & Autorisierung

### 4.1 Plugin-API-Keys (neu)
- Jedes registrierte Plugin erhält einen oder mehrere **API-Keys** (Format: `jvp_<plugin>_<rand>`).
- Übertragung im Header: `Authorization: Bearer jvp_…`.
- Keys werden **nur als Hash** gespeichert (analog `auth._make_hash`), beim Erstellen einmalig
  im Klartext angezeigt. Rotation/Revoke im Admin-UI.
- Optional pro Key ein **Ablaufdatum** und eine **IP-/Netz-Allowlist** (LAN-only Default,
  passend zu `fetch_allow_lan`).

### 4.2 Nutzer-Kontext durchreichen
Ein Plugin agiert im Auftrag eines Nutzers. Zwei Wege, den Nutzer zu bestimmen:
1. **Header `X-JARVIS-User`** (Username) — vom Plugin gesetzt, **nur erlaubt**, wenn der Key das
   Scope `api:act_as_user` hat (vertrauenswürdiges First-Party-Plugin wie der ADHS-Helper).
2. **Delegierter Nutzer-Login**: Das Plugin leitet den Nutzer einmalig durch einen OAuth-artigen
   Consent-Flow (`POST /api/v1/auth/delegate`), erhält ein **nutzergebundenes Sub-Token**.
   Empfohlen für Dritt-Plugins.

Fehlt ein Nutzerkontext, läuft der Aufruf als **Gast** (`user_id=None`) — nur „offene“
Ressourcen erlaubt (vgl. `auth.is_tool_allowed`: Gäste dürfen nur ungelistete Ressourcen).

### 4.3 Scopes ↔ bestehendes Rechtemodell
Scopes sind Strings im selben Namensraum wie `group_permissions.resource`. Ein API-Key trägt eine
Menge gewährter Scopes; zusätzlich muss der **handelnde Nutzer** das Recht haben (UND-Verknüpfung).

| Scope | Erlaubt | Mappt auf bestehende Prüfung |
|---|---|---|
| `api:llm` | Inference/Chat/Structure | — (neu) |
| `api:vision` | Bildanalyse | — (neu) |
| `api:stt` / `api:tts` | Audio-Pipelines | — (neu) |
| `api:rag` | RAG insert/query in eigener Collection | Namespace-gebunden |
| `api:storage` | Plugin-KV/Doc-Store | Namespace-gebunden |
| `api:notify` | `announce()` / Telegram / Satellit | — (neu) |
| `api:events` | Event-Bus pub/sub | — (neu) |
| `api:scheduler` | Geplante Jobs anlegen | — (neu) |
| `tool:<name>` | Bestimmtes JARVIS-Tool aufrufen | `auth.is_tool_allowed` (bestehend) |
| `mcp:<server>` | Bestimmten MCP-Server nutzen | `auth.is_tool_allowed` (bestehend) |
| `api:act_as_user` | Nutzer per Header setzen | nur First-Party |
| `plugin:<id>` | Plugin ist aktiviert (An-/Aus) | analog `mcp:<server>` |

**Durchsetzung** (Gateway-Middleware, Pseudocode):
```python
key = parse_bearer(request)                 # → plugin_id, scopes, user_binding
require(scope in key.scopes)                # Gateway-Ebene
user_id = resolve_user(request, key)        # Header/Sub-Token/Gast
if scope.startswith(("tool:", "mcp:")):
    require(auth.is_tool_allowed(user_id, scope))   # Defense in Depth
namespace = f"plugin:{key.plugin_id}:u{user_id or 'guest'}"
```

---

## 5. REST-Schnittstellen (`/api/v1/…`)

Allgemeines:
- Basis-URL: `https://<host>:8088/api/v1` (LAN, TLS via vorhandene `certs/`).
- Auth: `Authorization: Bearer jvp_…` (+ optional `X-JARVIS-User`).
- Content-Type: `application/json`, außer Audio-Upload (`multipart/form-data`).
- Fehlerformat: siehe Anhang B. Versionierung: Header `X-JARVIS-API: v1`.

### 5.1 Inference Hub — `api:llm`

**`POST /api/v1/inference/chat`** — generischer Chat, optional gestreamt (SSE).
Backed by `services.llm_stream` (stream) bzw. `services.chat` (nicht-stream).
```jsonc
// Request
{
  "system_prompt": "Du bist ein deeskalierender ADHS-Coach …",
  "messages": [{"role": "user", "content": "Kind verweigert Socken."}],
  "model": "local-default",        // optional; default = config.llm_model
  "think": false,                  // adaptive Denkphase erzwingen/abschalten
  "stream": true,
  "max_tokens": 512
}
```
- `stream:true` → **SSE** mit Events `sentence` (satzweise, TTS-tauglich), `done`, `error`
  (exakt das Format aus `app.chat_stream` / `services.llm_stream`).
- `stream:false` → `{ "reply": "…", "model": "qwen3-14b" }`.

**`POST /api/v1/inference/structure`** — freien Text in striktes JSON wandeln (Micro-Task-Parser).
Intern: ein `llm_call` mit erzwungenem JSON-Schema + Validierung/Repair-Schleife.
```jsonc
// Request
{
  "text": "Morgenroutine für Lukas, um 7:45 muss er am Bus sein.",
  "schema": { "type": "object", "properties": {
      "target_time": {"type": "string"},
      "steps": {"type": "array", "items": {"type": "object", "properties": {
          "order": {"type":"integer"}, "task": {"type":"string"}, "duration": {"type":"integer"}}}}}}
}
// Response
{ "target_time": "07:45",
  "steps": [ {"order":1,"task":"Anziehen (Hose & Shirt)","duration":5},
             {"order":2,"task":"Frühstücken","duration":15} ] }
```

**`POST /api/v1/inference/agent`** — voller Agenten-Tool-Loop (LLM darf JARVIS-Tools nutzen).
Backed by `app._run_loop` + `tools.execute_tool`. Nur Tools, für die Key **und** Nutzer Rechte
haben (`tool:*`-Scopes). Für Plugins, die JARVIS „etwas erledigen lassen“ wollen statt nur Text.
```jsonc
{ "task": "Lege Lukas' Morgenroutine als Aufgaben an und stelle einen Timer.",
  "allow_tools": ["add_todo", "set_timer"],   // Teilmenge; sonst alle erlaubten
  "user": "Vater" }                            // statt X-JARVIS-User
```

### 5.2 Vision — `api:vision`

**`POST /api/v1/vision/analyze`** — Bild beschreiben/beantworten.
Backed by `services.vision_call`. Wichtig: Der GPU-Server hat **kein Internet** → Bilder werden
als **base64/Upload** übergeben, nicht als externe URL (siehe Memory „Vision & Recherche“).
```jsonc
// Variante A: JSON mit data-URL
{ "question": "Welche Medikamentenschachtel ist das? Lies die Dosierung.",
  "image": "data:image/jpeg;base64,/9j/4AAQ…" }
// Variante B: multipart/form-data: file=<bild>, question=<text>
// Response
{ "answer": "Methylphenidat 10 mg, 1-0-0 …", "model": "gemma4-12b" }
```
**`POST /api/v1/vision/ocr`** *(optional, Convenience)* — reiner Text-Extrakt aus Bild
(spezialisierter Prompt). Nützlich generisch (Belege, Etiketten, Hausaufgaben).
**`POST /api/v1/vision/classify`** *(optional)* — Bild gegen vorgegebene Label-Liste.
```jsonc
{ "image": "data:image/jpeg;base64,…", "labels": ["aufgeräumt","unaufgeräumt"] }
// → { "label": "unaufgeräumt", "scores": {"aufgeräumt":0.2,"unaufgeräumt":0.8} }
```
> So deckt die Vision-Schnittstelle nicht nur den ADHS-Helper ab, sondern jedes künftige
> Bilderkennungs-Plugin (Türklingel-Kamera, Kühlschrank-Inventar, Dokumenten-Scan).

### 5.3 Audio STT — `api:stt`
**`POST /api/v1/audio/stt`** — `multipart/form-data` (`file`=WAV/MP3/OGG, `language` opt.).
Backed by `services.transcribe`. → `{ "text": "…", "language": "de" }`.

### 5.4 Audio TTS — `api:tts`
**`POST /api/v1/audio/tts`** — `{ "text": "Zeit für Schritt zwei!", "voice": "edge|piper|kokoro", "format": "wav" }`.
Backed by `services.synthesize`. → Binärstream `audio/wav` (bzw. ogg).
Alternativ `?deliver=announce&session_id=…` → direkt auf einem Satelliten/Browser ausspielen
(siehe 5.6) statt Bytes zurückzugeben.

### 5.5 RAG / Gedächtnis — `api:rag`
Backed by `knowledge.py` + `store.py`. **Collection** = logischer Name; physisch gemappt auf
`kind="document"` (bzw. eigener kind `"plugin"`) mit `namespace = plugin:<id>:u<uid>:<collection>`.
So sind Plugin-Daten von Core-Gedächtnis und anderen Plugins getrennt.

**`POST /api/v1/rag/insert`**
```jsonc
{ "collection": "adhs_family_logs",
  "content": "Medikation 07:30 genommen. Rebound gegen 16:15 heftig.",
  "metadata": {"user":"Vater","tags":["Medikation","Rebound"],"ts":"2026-06-25T16:15:00"} }
// → { "id": "…", "chunks": 1 }
```
**`POST /api/v1/rag/query`**
```jsonc
{ "collection": "adhs_family_logs", "query": "Wie liefen Nachmittage mit früher Medikation?",
  "limit": 5, "min_score": 0.3 }
// → { "results": [ {"content":"…","score":0.81,"metadata":{…}}, … ] }
```
**`POST /api/v1/rag/ingest`** — größeres Dokument (Chunking automatisch, `knowledge.ingest_document`).
**`DELETE /api/v1/rag/source`** — `{ "collection":"…", "source":"…" }` (`store.delete_source`).

### 5.6 Channels / Notify — `api:notify`
Backed by `app.announce` + `messaging`. Das Plugin muss den Gerätetyp **nicht** kennen.

**`POST /api/v1/channels/notify`**
```jsonc
{ "user": "Vater",                       // oder "session_id": "…", oder "channels": [...]
  "channels": ["auto"],                  // auto = aktive Session(s); sonst telegram|satellite|browser
  "priority": "high",                    // low|normal|high
  "text": "⚠️ Erinnerung: Medikation einnehmen!",
  "kind": "reminder",                    // landet als announce-Event-Typ
  "speak": true,                         // auf Sprachgeräten zusätzlich vorlesen
  "meta": {"plugin":"adhs","task_id":"123"} }
// → { "delivered": true, "spoken": true, "targets": ["satellite_livingroom","telegram"] }
```
Routing-Regeln (Server entscheidet):
- `user` → alle aktiven Sessions des Nutzers (`hub.sessions_for_user`) + optional Telegram.
- `channels:["telegram"]` → `messaging.send_to_user`.
- `channels:["satellite"|"browser"]`/`session_id` → `announce()` mit kanal-bewusstem TTS.

**`POST /api/v1/channels/photo`** — Bild an Telegram (`messaging.send_photo_to_user`).

### 5.7 Plugin-Storage (KV/Doc) — `api:storage` *(neu)*
Damit ein Plugin **keine eigene DB** braucht (Tamagotchi-Zustand, XP, Tasks, Profile).
Neue Tabelle `plugin_kv(plugin_id, namespace, key, value JSONB, updated_at)`.
Namespace default `plugin:<id>:u<uid>` (pro Nutzer) oder `plugin:<id>:shared` (Familien-Pool).

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/api/v1/storage/{collection}/{key}` | Wert lesen |
| `PUT` | `/api/v1/storage/{collection}/{key}` | Wert setzen (JSON-Body) |
| `PATCH` | `/api/v1/storage/{collection}/{key}` | Teil-Merge / atomares Inkrement (`{"$inc":{"xp":50}}`) |
| `DELETE`| `/api/v1/storage/{collection}/{key}` | löschen |
| `GET` | `/api/v1/storage/{collection}` | Liste/Query (`?prefix=`, `?limit=`) |

`?scope=shared` schaltet auf den geteilten Familien-Namespace (kooperatives Tamagotchi).

### 5.8 Tools-Bridge — `tool:<name>`
**`GET /api/v1/tools`** → Liste der für Key+Nutzer erlaubten JARVIS-Tools (Schema aus
`tools.TOOL_SCHEMAS` + Skills + MCP, gefiltert).
**`POST /api/v1/tools/{name}/invoke`** → `tools.execute_tool(name, args, ctx)` direkt.
```jsonc
{ "args": {"label":"Zähneputzen","seconds":120}, "user":"Lukas" }
// → { "result": "Timer „Zähneputzen" für 2 Minuten gestellt." }
```
Das gibt dem Plugin gezielten Zugriff auf bestehende Fähigkeiten (Timer, To-dos, Kalender,
Smarthome via MCP) **ohne** sie nachzubauen.

### 5.9 Scheduler — `api:scheduler` *(neu, dünne Fassade über `automations.manager`)*
**`POST /api/v1/scheduler/jobs`** — geplanten/wiederkehrenden Job anlegen.
```jsonc
{ "title": "Sanfter Neustart Tamagotchi",
  "schedule": {"cron": "0 6 * * *"},          // oder {"in_seconds": 1800} / {"at": "ISO"}
  "action": {"type":"event","event":"adhs.daily_reset"},   // feuert Plugin-Event
  "owner_user": "Vater" }
// → { "id": "job_…", "next_run": "2026-06-26T06:00:00" }
```
`action.type` ∈ `event` (Plugin bekommt’s über WS), `notify` (direkte Erinnerung), `webhook`
(POST an eine vom Plugin registrierte Callback-URL), `agent` (autonomer Tool-Loop).
`GET/DELETE /api/v1/scheduler/jobs[/{id}]` zum Verwalten.

### 5.10 Identität & Nutzer — `api:users` *(lesend, restriktiv)*
**`GET /api/v1/me`** → `{ "user": "Vater", "user_id": 4, "scopes":[…], "plugin":"adhs" }`.
**`GET /api/v1/users`** → Liste der Nutzer, die das Plugin bedienen darf (nur mit
`api:act_as_user`; Familienmitglieder für den ADHS-Helper). Keine Passwörter/PII über das Nötige hinaus.

---

## 6. Echtzeit: Plugin-Event-WebSocket (`/api/v1/ws`)

Analog zu den bestehenden `/ws`, `/ws/client`, `/ws/satellite`. Auth beim Connect via
`?token=jvp_…` oder `Authorization`-Header. Nach Connect kann das Plugin **Topics abonnieren**
und **eigene Events publizieren**. Brücke zu `automations.emit` (eingehend) und zu einem neuen
Topic-Fanout (ausgehend an Plugins).

**Topic-Namensschema:** `jarvis/plugin/<plugin_id>/<bereich>/<event>`
sowie Core-Topics `jarvis/core/<event>` (z. B. `device_connected`, `timer_elapsed`).

**Client → Server**
```jsonc
{ "op": "subscribe", "topics": ["jarvis/plugin/adhs/#", "jarvis/core/device_connected"] }
{ "op": "publish",   "topic": "jarvis/plugin/adhs/gamification/xp",
                     "payload": {"delta": 50, "source": "Lukas", "total": 1200} }
{ "op": "ping" }
```
**Server → Client**
```jsonc
{ "op": "event", "topic": "jarvis/core/timer_elapsed", "payload": {"label":"Zähneputzen"} }
{ "op": "event", "topic": "jarvis/plugin/adhs/timer/tick", "payload": {"remaining": 42} }
```

**Eingehende Sprachbefehle:** Spricht jemand „Jarvis, …“ in ein Satelliten-Mikro und der Befehl
ist einem Plugin zugeordnet, kann JARVIS ihn als Event `jarvis/plugin/<id>/intent` zustellen
(Intent-Routing optional in Phase 3). Standardweg bleibt: Plugin nutzt aktiv STT (5.3).

**Wichtig (Doppel-TTS-Vermeidung):** Ausgaben **nie** direkt auf Geräte schreiben — immer über
`/api/v1/channels/notify` (5.6) bzw. `announce()`, das `hub.render_mode()` respektiert.

---

## 7. Plugin-Registry & Manifest

Analog `mcp_hub.py`: Plugins werden in DB-Tabellen registriert und im **Admin-UI im Tab
„🧩 Plugins"** verwaltet — ✅ implementiert (registrieren, aktivieren/deaktivieren, API-Keys
erzeugen/widerrufen, Scopes per Checkbox, Nutzerbindung). Registrierung über ein **Manifest**.

**`plugin.json` (Manifest-Schema):**
```jsonc
{
  "id": "adhs",                         // eindeutig, kebab/lowercase; bildet plugin:<id>
  "name": "ADHS-Family-Helper",
  "version": "1.0.0",
  "type": "external",                   // external | internal | hybrid
  "description": "Neurodiverses Familien-Assistenzsystem (PWA).",
  "author": "…",
  "scopes_requested": ["api:llm","api:vision","api:stt","api:tts","api:rag",
                        "api:storage","api:notify","api:events","api:scheduler",
                        "api:act_as_user","tool:add_todo","tool:set_timer"],
  "events_published": ["gamification/xp","timer/tick","daily_reset"],
  "events_subscribed": ["jarvis/core/device_connected","jarvis/core/timer_elapsed"],
  "storage_collections": ["tamagotchi","tasks","profiles"],
  "rag_collections": ["adhs_family_logs"],
  "webhook_url": "https://adhs.local/hooks",   // optional, für scheduler/webhook
  "ui": { "entry": "https://adhs.local/", "icon": "🧩", "embed_in_admin": false },
  "internal": { "module": "plugins.adhs", "min_core_version": "0.2.0" }  // nur type internal/hybrid
}
```
Registrierungs-Endpunkte (Admin, Cookie-Auth):
- `GET /api/admin/plugins` · `POST /api/admin/plugins` (Manifest hochladen/registrieren)
- `POST /api/admin/plugins/{id}/keys` (Key erzeugen → einmalig Klartext) · `…/keys/{kid}/revoke`
- `POST /api/admin/plugins/{id}/scopes` (Scopes gewähren/entziehen)
- `POST /api/admin/plugins/{id}/enable|disable`
- `GET  /api/admin/plugins/{id}/health` (Heartbeat/letzter Call/Fehlerquote)

---

## 8. Plugin-Template & Voraussetzungen

### 8.1 Voraussetzungen (für Typ A / externes Plugin)
- Spricht HTTPS gegen `https://<jarvis-host>:8088/api/v1` im LAN (selbstsigniertes Zert akzeptieren
  oder JARVIS-CA importieren).
- Besitzt einen **Plugin-API-Key** (vom Admin im UI erzeugt).
- Kennt seine **`id`** und seinen **Namespace** (`plugin:<id>`).
- Hält keine Geheimnisse von JARVIS außer dem Key; Nutzerdaten bleiben in JARVIS-Namespaces.
- PWA-Vorgaben (aus dem ADHS-Material): `manifest.json`, Service-Worker + Web-Push,
  Standalone-Modus, Dark-Mode, Zero-Text-Entry, Vibration-API, „Vergebungsmodus“ (keine roten Fehler).

### 8.2 Verzeichnis-Template (externes Plugin)
```
adhs-helper/
├─ plugin.json            # Manifest (Kapitel 7)
├─ web/                   # PWA
│  ├─ index.html
│  ├─ manifest.json       # PWA-Manifest (Homescreen, Standalone)
│  ├─ sw.js               # Service-Worker (Web-Push, Offline-Cache)
│  └─ src/ …              # UI, Canvas-Tamagotchi, Timer-Scheibe
├─ jarvis-client.js       # dünner SDK-Wrapper um /api/v1 (Beispiel unten)
└─ README.md
```

**Minimaler JS-Client (Auszug, copy-paste-fähig):**
```js
const JARVIS = "https://192.168.66.224:8088/api/v1";
const KEY = "jvp_adhs_…";                    // aus Admin-UI
const H = (u) => ({ "Authorization": `Bearer ${KEY}`, "X-JARVIS-User": u });

// 1) Sprachnotiz → Text
async function stt(file, user) {
  const fd = new FormData(); fd.append("file", file);
  const r = await fetch(`${JARVIS}/audio/stt`, { method:"POST", headers:H(user), body:fd });
  return (await r.json()).text;
}
// 2) Freitext → Mikro-Schritte (JSON)
async function microtasks(text, user) {
  const r = await fetch(`${JARVIS}/inference/structure`, {
    method:"POST", headers:{...H(user), "Content-Type":"application/json"},
    body: JSON.stringify({ text, schema: TASK_SCHEMA }) });
  return r.json();
}
// 3) Log ins RAG-Tagebuch
async function logDiary(content, meta, user) {
  await fetch(`${JARVIS}/rag/insert`, { method:"POST",
    headers:{...H(user), "Content-Type":"application/json"},
    body: JSON.stringify({ collection:"adhs_family_logs", content, metadata:meta }) });
}
// 4) Tamagotchi-XP gemeinsam erhöhen (geteilter Namespace)
async function addXP(delta, user) {
  await fetch(`${JARVIS}/storage/tamagotchi/state?scope=shared`, { method:"PATCH",
    headers:{...H(user), "Content-Type":"application/json"},
    body: JSON.stringify({ "$inc": { xp: delta } }) });
}
// 5) Live-Events (XP-Ticks, Timer) für alle Geräte
const ws = new WebSocket(`wss://192.168.66.224:8088/api/v1/ws?token=${KEY}`);
ws.onopen = () => ws.send(JSON.stringify({ op:"subscribe", topics:["jarvis/plugin/adhs/#"] }));
ws.onmessage = (e) => renderTamagotchi(JSON.parse(e.data));
// 6) Sanfte Erinnerung dorthin, wo der Nutzer ist
async function nudge(text, user) {
  await fetch(`${JARVIS}/channels/notify`, { method:"POST",
    headers:{...H(user), "Content-Type":"application/json"},
    body: JSON.stringify({ user, channels:["auto"], speak:true, kind:"reminder", text }) });
}
```

### 8.3 Template (internes Plugin, Typ B) — siehe Kapitel 9.

---

## 9. Server-Plugin-Loader (Typ B, optional)

Für serverseitige Logik. Modul unter `orchestrator/plugins/<id>/__init__.py`, beim Start vom
**Plugin-Loader** (neu, analog `mcp_hub.init`/`skills`) geladen. Ein Plugin implementiert eine
`register(api)`-Funktion und bekommt ein schlankes, **stabiles `PluginAPI`-Objekt** (Fassade,
damit interne Refactorings es nicht brechen).

```python
# orchestrator/plugins/adhs/__init__.py
META = {"id": "adhs", "name": "ADHS-Family-Helper", "version": "1.0.0"}

def register(api):
    # eigene Tools für den Sprachassistenten beisteuern
    @api.tool("tamagotchi_status", "Status des Familien-Tamagotchi abfragen.",
              params={"type":"object","properties":{}})
    async def status(args, ctx):
        st = await api.storage.get("tamagotchi", "state", scope="shared")
        return f"Das Familientier ist Level {st['level']} mit {st['xp']} XP."

    # eigene REST-Routen unter /api/v1/plugin/adhs/*
    @api.route("POST", "/tasks")
    async def add_task(req, ctx):
        ...

    # auf Core-Events reagieren
    @api.on_event("timer_elapsed")
    async def on_timer(payload):
        await api.notify(user=payload.get("user"), text="Schritt geschafft! +20 XP",
                         kind="gamification", speak=True)
        await api.storage.patch("tamagotchi", "state", {"$inc": {"xp": 20}}, scope="shared")

    # geplante Jobs
    api.schedule(cron="0 6 * * *", event="adhs.daily_reset", title="Sanfter Neustart")
```

**`PluginAPI`-Oberfläche (stabile Fassade über bestehende Module):**

| `api.*` | delegiert an | Zweck |
|---|---|---|
| `api.tool(name, desc, params)` | `tools.TOOL_SCHEMAS` + Dispatch | Tool für Assistenten registrieren |
| `api.route(method, path)` | FastAPI-Subrouter `/api/v1/plugin/<id>` | eigene REST-Endpunkte |
| `api.on_event(name)` | `automations`-Eventbus | Core-/Plugin-Events abonnieren |
| `api.emit(topic, payload)` | Event-Fanout + WS | Plugin-Event feuern |
| `api.schedule(...)` | `automations.manager` | geplante Jobs |
| `api.llm`, `api.vision`, `api.stt`, `api.tts` | `services.*` | KI-Dienste |
| `api.rag` | `knowledge.py` | insert/query (namespaced) |
| `api.storage` | `store.py` / `plugin_kv` | KV/Doc |
| `api.notify(...)` | `announce()` / `messaging` | Rückkanal |
| `api.config(key)` | `config.get()` | nur Plugin-eigene Settings |

Sicherheits-Leitplanken für Typ B: Plugins laufen im Orchestrator-Prozess (vertrauenswürdig,
First-Party). Untrusted Code gehört in die **Sandbox** (`sandbox.py`/`deploy/sandbox`), nicht hier.
Loader respektiert `enable/disable` aus der Registry; Fehler beim Laden eines Plugins dürfen den
Core **nicht** crashen (try/except pro Plugin, wie `_startup` heute MCP behandelt).

---

## 10. Datenmodell & Persistenz (neu)

```sql
CREATE TABLE plugins (
  id TEXT PRIMARY KEY, name TEXT, version TEXT, type TEXT,
  manifest JSONB, enabled BOOL DEFAULT true, created_at TIMESTAMPTZ DEFAULT now());

CREATE TABLE plugin_api_keys (
  kid TEXT PRIMARY KEY, plugin_id TEXT REFERENCES plugins(id),
  hash TEXT, salt TEXT, scopes TEXT[], user_binding INT NULL,
  expires_at TIMESTAMPTZ NULL, ip_allow TEXT[] NULL,
  last_used TIMESTAMPTZ, revoked BOOL DEFAULT false);

CREATE TABLE plugin_kv (
  plugin_id TEXT, namespace TEXT, collection TEXT, key TEXT,
  value JSONB, updated_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (plugin_id, namespace, collection, key));
```
- **RAG** nutzt die bestehende `vectors`-Tabelle (`store.py`) mit `kind='plugin'` und
  `namespace='plugin:<id>:u<uid>:<collection>'` — keine neue Vektor-Infrastruktur nötig.
- **Scopes** liegen redundant am Key (schnelle Prüfung) und werden mit `group_permissions`
  (Nutzerseite) UND-verknüpft.

---

## 11. Sicherheit & Betrieb

- **Least Privilege:** Plugin bekommt nur angefragte+gewährte Scopes; Admin bestätigt im UI.
- **Key-Hygiene:** Hash-only Speicherung, Rotation, Ablauf, Revoke, `last_used`-Telemetrie.
- **LAN-Default:** API-Keys standardmäßig auf private Netze beschränkt (`ip_allow`, vgl.
  `fetch_allow_lan`). Externe Exposition nur bewusst.
- **Rate-Limiting** pro Key (z. B. Token-Bucket) — schützt LLM-/GPU-Ressourcen (1 Slot je Modell,
  siehe `config.models`).
- **Autonomie-Parität:** `inference/agent` und Tool-Bridge respektieren
  `autonomous_tool_blacklist`/`autonomous_mcp_blacklist`, wenn ohne Live-Nutzer aufgerufen.
- **PII/Datenschutz:** RAG-Logs (z. B. Medikation, Verhaltensmuster) sind **hochsensibel** —
  strikt pro Nutzer-Namespace, kein Cross-Plugin-Zugriff, lokal (kein Cloud-Egress; GPU-Server
  hat ohnehin kein Internet).
- **Audit:** Alle Plugin-Calls über `debug.log` (Tool-Mitschnitt existiert bereits).
- **Telegram-Vertrauensanker bleibt:** Nachrichten gehen weiter nur an **verifizierte** Chats
  (`messaging.is_verified`) — Plugins können diesen Chokepoint nicht umgehen.

---

## 12. Konkretes Mapping: ADHS-Helper → Schnittstellen

Beispiel-Workflow (aus dem Material): Vater spricht unterwegs in die PWA.

| Schritt | Plugin-Aktion | JARVIS-Schnittstelle | Backend |
|---|---|---|---|
| 1 | Sprachnotiz hochladen | `POST /api/v1/audio/stt` | `services.transcribe` |
| 2 | Kontext holen | `POST /api/v1/rag/query` (`adhs_family_logs`) | `knowledge.search_*` |
| 3 | Fokus-Plan erzeugen | `POST /api/v1/inference/structure` (+ RAG-Kontext) | `services.llm_call` |
| 4 | Plan/Tasks speichern | `PUT /api/v1/storage/tasks/<id>` | `plugin_kv` |
| 5 | Timer starten | `POST /api/v1/tools/set_timer/invoke` | `tools.execute_tool` |
| 6 | +50 XP gemeinsam | `PATCH /api/v1/storage/tamagotchi/state?scope=shared` | `plugin_kv` |
| 7 | Live-Sync alle Geräte | WS `publish jarvis/plugin/adhs/gamification/xp` | Event-Fanout |
| 8 | Sanfte Erinnerung später | `POST /api/v1/scheduler/jobs` → `notify` | `automations.manager` → `announce` |

Feature-Abdeckung:
- **Micro-Task-Parser** → `inference/structure`.
- **Neuro-Timer (Schrumpfscheibe)** → `tools/set_timer` + WS `timer/tick` (`announce`-Events).
- **Kooperatives Tamagotchi** → `storage … ?scope=shared` + WS `gamification/xp`; nie „sterben“ →
  reine Plugin-Logik, JARVIS speichert nur Zustand.
- **RAG-Tagebuch** → `audio/stt` + `rag/insert`/`rag/query` (pro Familienmitglied namespaced).
- **Mental-Load-Dämpfer / Erinnerungen** → `scheduler/jobs` + `channels/notify` (kanal-bewusst,
  Telegram + Satellit + PWA-Push).
- **Empathischer Eltern-Coach** → `inference/chat` (stream, eigener `system_prompt`).

---

## 13. Implementierungs-Roadmap (JARVIS-Core-Seite)

**Phase 0 — Fundament (Auth & Gateway-Gerüst)**
- `plugins`-Modul: Registry + `plugin.json`-Loader; Tabellen aus Kapitel 10 (`store.init`-Erweiterung).
- Plugin-API-Key-Auth (Erzeugen/Hash/Verify/Revoke) + Gateway-Middleware (Bearer + Scope-Check).
- Admin-UI-Tab „🔌 Plugins“ (Registrieren, Keys, Scopes, Enable/Disable, Health).
- Dateien: neu `orchestrator/plugins_registry.py`, `orchestrator/api_v1.py` (APIRouter);
  Anpassungen in `app.py` (`include_router`, `_startup`), `auth.py` (Scope-Helfer), `store.py`.

**Phase 1 — Kern-Schnittstellen (deckt ADHS-Helper voll ab)**
- `inference/{chat,structure,agent}`, `vision/analyze`, `audio/{stt,tts}`, `rag/{insert,query}`.
- Plugin-Storage (5.7), `channels/notify` (5.6).
- → Ab hier ist der **ADHS-Helper extern entwickelbar** (nur diese Endpunkte nötig).

**Phase 2 — Echtzeit & Automatisierung**
- Plugin-Event-WS (`/api/v1/ws`) + Topic-Fanout, Brücke zu `automations.emit`.
- Scheduler-Fassade (5.9) über `automations.manager`.
- Tools-Bridge (5.8), `vision/{ocr,classify}`.

**Phase 3 — Server-Plugins & Komfort**
- Server-Plugin-Loader + `PluginAPI` (Kapitel 9).
- Delegierter Nutzer-Consent-Flow (4.2 Variante 2), Rate-Limiting, Intent-Routing in Plugins.

---

## 14. Einsatz & Schnelleinstieg (implementiert)

### 14.1 Implementierungsstatus — neue/geänderte Dateien
| Datei | Inhalt |
|---|---|
| `orchestrator/plugins_registry.py` | Registry, API-Keys (Hash-only), Scopes, KV-/Doc-Store, Tabellen `plugins`/`plugin_api_keys`/`plugin_kv` |
| `orchestrator/plugin_bus.py` | In-Process Pub/Sub für den Event-WebSocket + Core-Event-Bridge |
| `orchestrator/api_v1.py` | Gateway-Router `/api/v1/*` (Inference, Vision, Audio, RAG, Storage, Notify, Tools, Scheduler, Identität, Event-WS) |
| `orchestrator/app.py` | `include_router(api_v1.router)`, Hooks (`announce`/Agent/Tools/Scheduler), Plugin-Admin-Endpunkte, Startup-Verdrahtung |
| `orchestrator/automations.py` | `add_listener()` + Listener-Aufruf in `emit()` (Core-Events → Bus); Plugin-Jobs im Runner |
| `orchestrator/tests/test_plugins.py` | DB-Tests (Registry/Keys/Scopes/KV/Bus), kein LLM nötig |
| `deploy/plugin-example/` | Lauffähiges Plugin-Template (Manifest + JS-Client + Quickstart) |

Alles getestet: `python3 tests/test_plugins.py` (23 Checks) + Live-Smoke gegen `:8088`
(REST, Auth/Scopes, Storage, RAG, LLM-Chat/Structure, Scheduler, WebSocket, Core-Event-Bridge).

### 14.2 Admin-Quickstart — Plugin registrieren & Key erzeugen

**Der einfache Weg (UI):** Admin-UI öffnen → `https://HOST:8088/admin` → anmelden → Tab **🧩 Plugins**.
Dort:
1. **Registrieren:** `id` (z. B. `adhs`) + Anzeigename eingeben → **Registrieren**. (Oder ein
   vollständiges `plugin.json` per „JSON registrieren" einfügen.)
2. **Key erzeugen:** beim Plugin auf **🔑 Keys** → „➕ Neuen API-Key erzeugen" → Scopes ankreuzen,
   optional Nutzer binden (z. B. `Vater`) → **Key erstellen**. Der Token wird **genau einmal**
   angezeigt — sofort kopieren und in der PWA hinterlegen.
3. Hier ebenso: Keys widerrufen, Plugin (de)aktivieren oder löschen.

**Der Skript-Weg (API):** dieselben Aktionen über die Cookie-geschützten Endpunkte
(`jarvis_admin_token`, Login über `/admin`). Mit gültigem Admin-Cookie (`-b cookies.txt`):

```bash
# 1) Plugin aus Manifest registrieren
curl -sk -b cookies.txt -X POST https://HOST:8088/api/admin/plugins \
  -H 'Content-Type: application/json' \
  -d '{"id":"adhs","name":"ADHS-Family-Helper","version":"1.0.0","type":"external"}'

# 2) API-Key erzeugen (Scopes + optional an Nutzer binden) → Token NUR EINMALIG im Klartext
curl -sk -b cookies.txt -X POST https://HOST:8088/api/admin/plugins/adhs/keys \
  -H 'Content-Type: application/json' \
  -d '{"label":"pwa","user":"Vater","scopes":["api:llm","api:vision","api:stt","api:tts",
       "api:rag","api:storage","api:notify","api:events","api:scheduler","tool:set_timer"]}'
# → {"ok":true,"token":"jvp_adhs_…"}   ← in der PWA hinterlegen

# 3) Übersicht / Schlüssel / Scopes ändern / widerrufen
curl -sk -b cookies.txt https://HOST:8088/api/admin/plugins
curl -sk -b cookies.txt https://HOST:8088/api/admin/plugins/adhs/keys
curl -sk -b cookies.txt -X POST https://HOST:8088/api/admin/plugins/keys/scopes -d '{"kid":"k_…","scopes":[…]}'
curl -sk -b cookies.txt -X POST https://HOST:8088/api/admin/plugins/keys/revoke -d '{"kid":"k_…"}'
```
Admin-Endpunkte (alle Cookie-Auth): `GET/POST /api/admin/plugins`,
`POST /api/admin/plugins/enable|delete`, `GET/POST /api/admin/plugins/{id}/keys`,
`POST /api/admin/plugins/keys/revoke|scopes`. `GET /api/admin/plugins` liefert zusätzlich
`api_scopes`, `tool_resources` und `mcp_resources` (für die Scope-Auswahl im UI).

### 14.3 Plugin nutzt das Gateway (so funktioniert es live)
```bash
TOK="jvp_adhs_…"; B="https://HOST:8088/api/v1"; H="Authorization: Bearer $TOK"
curl -sk -H "$H" $B/me                                   # Kontext/Scopes
curl -sk -X PUT  -H "$H" -d '{"value":{"xp":0,"level":1}}' "$B/storage/tama/state?scope=shared"
curl -sk -X PATCH -H "$H" -d '{"$inc":{"xp":50}}'         "$B/storage/tama/state?scope=shared"
curl -sk -X POST -H "$H" -d '{"text":"Morgenroutine: 7:45 Bus.","schema":{…}}' $B/inference/structure
curl -sk -X POST -H "$H" -d '{"collection":"logs","content":"…","metadata":{}}' $B/rag/insert
curl -sk -X POST -H "$H" -d '{"text":"Erinnerung!","channels":["auto"],"speak":true}' $B/channels/notify
```

### 14.4 Verbindliche Implementierungsdetails (maßgeblich für Plugin-Autoren)
- **Auth-Header:** `Authorization: Bearer jvp_…`. Nutzer: `X-JARVIS-User: <name>` (nur mit Scope
  `api:act_as_user`), sonst die feste Key-Bindung, sonst Gast (`user_id=null`).
- **Storage-Body:** `PUT` akzeptiert `{"value": …}` **oder** direkt das Objekt als Body.
  `PATCH` nimmt `{"$inc":{"feld":n}}` (atomar) und/oder flache Felder zum Mergen.
  `?scope=user` (default, pro Nutzer) oder `?scope=shared` (Familien-Pool).
- **RAG:** physisch `kind='plugin'`, Namespace `plugin:<id>:u<uid>:<collection>` — vollständig
  isoliert je Plugin **und** Nutzer. Embeddings lokal (nomic, kein GPU nötig).
- **Scheduler `schedule`:** eines von `{"in_seconds":n}`, `{"at":"ISO8601"}`,
  `{"interval_seconds":n}`, `{"daily":"HH:MM"}`, `{"cron":"M H * * *"}` (nur einfache tägliche Cron).
  `action.type` ∈ `event` | `notify` | `webhook` | `agent`. Jobs liegen im `automations.manager`.
- **Event-WS:** `wss://HOST:8088/api/v1/ws?token=jvp_…` (Scope `api:events`). Server sendet zuerst
  `{"op":"ready"}`. Ops: `subscribe`/`publish`/`ping`. Publizieren **nur** unter
  `jarvis/plugin/<id>/…` (Fremd-Namespace → `{"op":"error"}`). Core-Events kommen als
  `jarvis/core/<event>` (z. B. `timer_elapsed`, `device_connected`, `document_uploaded`).
- **Notify-Antwort:** `{"delivered":bool,"spoken":bool,"targets":[…]}`. Ohne aktive Session/Telegram
  ist `delivered:false` normal (kein Fehler).
- **CORS (wichtig für Browser-PWAs):** Eine PWA läuft auf einem **anderen Origin/Port** — ohne CORS
  blockiert der Browser die Gateway-Aufrufe. Das Gateway sendet die nötigen CORS-Header (inkl.
  Preflight `OPTIONS`) für `/api/v1/*` automatisch. Erlaubte Origins: `config.plugin_cors_origins`
  (Default `["*"]`, da Bearer-Auth ohne Cookies) **plus** automatisch der `ui.entry`-Origin jedes
  registrierten Plugins. Zum Einschränken in `config.json` z. B.
  `"plugin_cors_origins": ["http://192.168.66.224:8096"]`. **Schnelldiagnose:** klappt
  `curl -k -H "Authorization: Bearer KEY" https://HOST:8088/api/v1/me`, scheitert aber der Browser
  → es ist CORS (Origin nicht erlaubt). WebSocket (`/api/v1/ws`) braucht kein CORS (Token im Query).
- **Fehler:** JSON `{"error":{"code","message","status"}}` bzw. FastAPI-`{"detail":…}` bei 401/403/400.

### 14.5 Tests ausführen
```bash
cd orchestrator
python3 tests/test_plugins.py     # Registry/Keys/Scopes/KV/Bus (Postgres nötig, kein LLM)
python3 tests/run_unit.py         # bestehende Unit-Tests (Regression)
```

---

## Anhang A — Vollständige Endpunkt-Referenz

| Methode | Pfad | Scope | Backend | Zweck |
|---|---|---|---|---|
| POST | `/api/v1/inference/chat` | `api:llm` | `services.llm_stream`/`chat` | Chat (SSE optional) |
| POST | `/api/v1/inference/structure` | `api:llm` | `services.llm_call` | Text → striktes JSON |
| POST | `/api/v1/inference/agent` | `api:llm`+`tool:*` | `app._run_loop` | Agenten-Tool-Loop |
| POST | `/api/v1/vision/analyze` | `api:vision` | `services.vision_call` | Bild beschreiben/fragen |
| POST | `/api/v1/vision/ocr` | `api:vision` | `services.vision_call` | Text aus Bild |
| POST | `/api/v1/vision/classify` | `api:vision` | `services.vision_call` | Bild → Label |
| POST | `/api/v1/audio/stt` | `api:stt` | `services.transcribe` | Sprache → Text |
| POST | `/api/v1/audio/tts` | `api:tts` | `services.synthesize` | Text → Audio |
| POST | `/api/v1/rag/insert` | `api:rag` | `knowledge.save/ingest` | Log/Memo speichern |
| POST | `/api/v1/rag/query` | `api:rag` | `knowledge.search_*` | Semantische Suche |
| POST | `/api/v1/rag/ingest` | `api:rag` | `knowledge.ingest_document` | Dokument chunked indexieren |
| DELETE | `/api/v1/rag/source` | `api:rag` | `store.delete_source` | Quelle löschen |
| GET/PUT/PATCH/DELETE | `/api/v1/storage/{coll}/{key}` | `api:storage` | `plugin_kv` | KV-Store |
| GET | `/api/v1/storage/{coll}` | `api:storage` | `plugin_kv` | Liste/Query |
| POST | `/api/v1/channels/notify` | `api:notify` | `announce`/`messaging` | Erinnerung/Alarm |
| POST | `/api/v1/channels/photo` | `api:notify` | `messaging.send_photo_to_user` | Bild senden |
| GET | `/api/v1/tools` | `tool:*` | `tools.TOOL_SCHEMAS` | erlaubte Tools |
| POST | `/api/v1/tools/{name}/invoke` | `tool:<name>` | `tools.execute_tool` | Tool ausführen |
| POST/GET/DELETE | `/api/v1/scheduler/jobs[/{id}]` | `api:scheduler` | `automations.manager` | geplante Jobs |
| GET | `/api/v1/me` | — | Registry | eigener Kontext |
| GET | `/api/v1/users` | `api:act_as_user` | `auth.list_users` | bedienbare Nutzer |
| WS | `/api/v1/ws` | `api:events` | Fanout + `automations` | Event-Bus |
| — | `/api/admin/plugins…` | Admin-Cookie | Registry | Verwaltung (Kapitel 7) |

## Anhang B — Konventionen

**Fehlerformat**
```jsonc
{ "error": { "code": "scope_denied", "message": "Scope api:rag fehlt für diesen Key.",
             "status": 403, "request_id": "…" } }
```
Codes: `unauthorized` (401), `scope_denied`/`user_not_allowed` (403), `not_found` (404),
`rate_limited` (429), `upstream_unavailable` (502, z. B. GPU-Server offline), `bad_request` (400).

**Versionierung:** Pfad-Präfix `v1`; additive Änderungen erlaubt, Breaking → `v2`.
Antworten tragen `X-JARVIS-API: v1` und `X-Plugin-Namespace: plugin:<id>:u<uid>`.

**Konsistenz mit Core:** Default-Sprache `de`; LLM-Default `config.llm_model`; Vision-Bilder als
base64/Upload (GPU-Server ohne Internet); Ausgaben kanal-bewusst über `announce()`.

---

*Nächster Schritt:* Phase 0+1 umsetzen → ab dann ist der ADHS-Helper als separates PWA-Projekt
allein gegen dieses Dokument entwickelbar.
