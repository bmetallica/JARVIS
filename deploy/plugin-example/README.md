# JARVIS Plugin-Template (Beispiel: ADHS-Family-Helper)

Lauffähiges Gerüst, um ein **externes Plugin** gegen das JARVIS Plugin-Gateway
(`/api/v1/*`) zu entwickeln — entkoppelt vom Core. Vollständige Spezifikation:
[`../../pluginsystem.md`](../../pluginsystem.md).

## Inhalt
| Datei | Zweck |
|---|---|
| `plugin.json` | Manifest (id, Scopes, Events, Collections) — beim Admin registrieren |
| `jarvis-client.js` | Schlanker SDK-Wrapper um alle Gateway-Endpunkte (Browser + Node) |
| `README.md` | Dieser Schnelleinstieg |

## Voraussetzungen
- JARVIS-Orchestrator läuft im LAN (`https://<host>:8088`, selbstsigniertes Zert → CA importieren
  oder im Dev `NODE_TLS_REJECT_UNAUTHORIZED=0`).
- Ein **Plugin-API-Key** vom Admin (siehe unten).

## In 3 Schritten einsatzbereit
1. **Registrieren** (Admin, mit gültigem `jarvis_admin_token`-Cookie):
   ```bash
   curl -sk -b cookies.txt -X POST https://HOST:8088/api/admin/plugins \
     -H 'Content-Type: application/json' --data-binary @plugin.json
   ```
2. **API-Key holen** (Token wird **nur einmal** im Klartext zurückgegeben):
   ```bash
   curl -sk -b cookies.txt -X POST https://HOST:8088/api/admin/plugins/adhs/keys \
     -H 'Content-Type: application/json' \
     -d '{"label":"pwa","user":"Vater","scopes":["api:llm","api:storage","api:notify",
          "api:rag","api:events","api:scheduler","api:stt","api:tts","tool:set_timer"]}'
   ```
3. **Loslegen** (Browser/Node):
   ```js
   const { JarvisClient } = require("./jarvis-client.js"); // im Browser: <script src=…>
   const jv = new JarvisClient("https://HOST:8088", "jvp_adhs_…", "Vater");

   // Mikro-Tasks aus Freitext
   const plan = await jv.structure("Morgenroutine: 7:45 am Bus. Anziehen 5min, Frühstück 15min.",
     { type:"object", properties:{ target_time:{type:"string"}, steps:{type:"array"} } });

   // Kooperatives Tamagotchi (Familien-Pool)
   await jv.storagePatch("tamagotchi", "state", { $inc: { xp: 50 } }, "shared");

   // RAG-Tagebuch
   await jv.ragInsert("adhs_family_logs", "Rebound gegen 16:15.", { user:"Vater", tags:["Medikation"] });

   // Sanfte Erinnerung dorthin, wo der Nutzer ist
   await jv.notify("Zeit für Schritt 2: Zähneputzen!", { speak:true });

   // Live-Sync über alle Geräte
   const bus = jv.connectEvents(["jarvis/plugin/adhs/#","jarvis/core/timer_elapsed"],
     (topic, p) => console.log("event", topic, p));
   bus.publish("jarvis/plugin/adhs/gamification/xp", { delta: 50 });

   // Geplanter „sanfter Neustart"
   await jv.scheduleJob("Daily Reset", { daily: "06:00" }, { type:"event", event:"daily_reset" });
   ```

## Scopes (mindestens nötig pro Feature)
| Feature | Scope |
|---|---|
| Chat/Coach, Mikro-Task-Parser | `api:llm` |
| Bild-/Beleg-/Etikettenerkennung | `api:vision` |
| Sprachnotiz → Text / Vorlesen | `api:stt` / `api:tts` |
| RAG-Tagebuch | `api:rag` |
| Tamagotchi/Tasks/Profile (KV) | `api:storage` |
| Erinnerungen (Push/Telegram/Satellit) | `api:notify` |
| Live-Events (XP/Timer-Ticks) | `api:events` |
| Geplante Nudges / Resets | `api:scheduler` |
| Timer/To-dos von JARVIS nutzen | `tool:set_timer`, `tool:add_todo` |
| Nutzer per Header wählen (First-Party) | `api:act_as_user` |

## CORS (Browser-PWA auf anderem Origin)
Die PWA läuft auf einem anderen Port als JARVIS → der Browser verlangt CORS. Das Gateway sendet die
Header automatisch für jeden in `config.plugin_cors_origins` (Default `["*"]`) gelisteten Origin
**und** für den `ui.entry`-Origin registrierter Plugins. Zum Einschränken in `config.json`:
```json
"plugin_cors_origins": ["http://192.168.66.224:8096"]
```
**Diagnose:** Klappt `curl -k -H "Authorization: Bearer KEY" https://HOST:8088/api/v1/me`,
scheitert aber der Browser → es ist CORS (Origin nicht erlaubt), nicht der Key.

## Hinweise
- Bilder immer als **base64/data-URL** senden (der GPU-Server hat kein Internet).
- Ausgaben nie direkt auf Geräte schreiben — immer `notify(...)`; JARVIS entscheidet kanal-bewusst
  (Browser/Pi sprechen lokal, ESP-Satellit bekommt serverseitiges TTS).
- Daten sind je Plugin **und** Nutzer isoliert (`scope:"user"`); für Familien-Features `scope:"shared"`.
