# SH-Jarvis — Bekannte & absehbare Probleme (mit Lösungen ohne neue Hardware)

> Stand: 2026-06-23. Bezug: Agenten-Kern (Orchestrator + LLM-Tool-Loop). Hardware-Rahmen:
> **2× NVIDIA RTX 3060 = 24 GB VRAM gesamt**. Alle Lösungen hier kommen **ohne Hardware-Erweiterung** aus
> (Modellwahl/Quantisierung/Software/Architektur). Priorität: 🔴 hoch · 🟡 mittel · 🟢 niedrig.
> Legende Status: `[offen]` · `[teilweise]` · `[erledigt]`.

---

## 1. 🔴 LLM ist mit der Menge/Auswahl der Werkzeuge überfordert  `[teilweise]`
**Symptom (real beobachtet):** Bei „welche Systeme haben Updates?" griff das Modell zum **falschen** Werkzeug
(Smart-Home/Domoticz-MCP statt dem DPM-Skill) und gab eine plausible **Fehl-Antwort**. Es gibt sehr viele
gleichzeitig sichtbare Tools: interne Tools + MCP-Server (Domoticz, 27 Tools) + Client-Aktionen (~37) + Skills.
Je mehr Tools, desto schlechter trifft ein kleines Modell die Auswahl.

**Ursachen:** (a) zu großer, flacher Tool-Namensraum im Prompt; (b) gemma-12b ist schwach in Tool-Auswahl
ohne Reasoning; (c) ähnlich klingende Tools („Systeme/Geräte").

**Lösungen ohne Hardware:**
- **Tool-Router / Tool-Subsetting (größter Hebel):** Vor dem eigentlichen Turn nur eine **kleine, relevante
  Teilmenge** an Tools sichtbar machen. Möglichkeiten: (1) ein leichter Vorab-Klassifizierer (kann dasselbe LLM
  in einem billigen Mini-Call sein) wählt 1–2 Tool-Gruppen; (2) Kategorien (zeit/web/smarthome/dpm/datei/…) und
  nur die passende laden — **genau das „deferred tools"-Muster, das die Skills schon nutzen**, auf MCP & Client-
  Aktionen ausweiten.
- **Deferred MCP/Client-Tools:** MCP-Server & Client-Aktionen NICHT alle als Schema einblenden, sondern als
  Katalog (Name+Zweck) + `load_*`-Meta-Tool on demand (wie Skills). Spart Tokens *und* reduziert Verwechslung.
- **Klarere, abgrenzende Beschreibungen:** z.B. DPM-Skill „**Patch-/Update-Status der Linux-SERVER** (nicht
  Smart-Home-Geräte)". Eindeutige Domänenwörter trennen.
- **Denken aktiv** bei tool-reichen Turns (siehe Punkt 2) — der wirksamste Sofort-Hebel.

---

## 2. 🔴 Tool-Auswahl nur MIT Reasoning zuverlässig — `adaptive` ohne Denken patzt  `[teilweise]`
**Symptom (verifiziert):** Mit `think=true` wählte das Modell **2/2 korrekt** den DPM-Skill und antwortete
sauber („17 Systeme mit Updates: pihole 102, Entwicklung 55 …"). Im `adaptive`-Schnell-Pass (ohne Denken)
wählte es wiederholt das falsche Tool. Eine plausible Fehl-Antwort eskaliert NICHT automatisch auf Denken
(nur Degenerations-Schleifen tun das jetzt).

**Lösungen ohne Hardware:**
- **`thinking_mode = auto`/`always`** für Agenten-/Skill-Aufgaben (im Admin-UI). Bei vorhandenem MCP heißt
  `auto` praktisch „Denken an". Empfohlener Default für dieses Setup.
- **Dev-/Agenten-Erkennung erweitern** (`_is_dev_request` + Session-Dev-Flag existieren bereits): zusätzlich
  Operativ-Begriffe (server, update, scan, skill-Namen) → Denken erzwingen. `[erledigt-teilweise]`
- **Eskalation auch bei „leerer/fehlgeschlagener" Tool-Antwort**, nicht nur bei Schleifen: wenn der Schnell-
  Pass nur eine Entschuldigung/Fehlermeldung produziert, einmal mit Denken nachfassen.

---

## 3. 🔴 Modell-Degeneration (Werkzeug-Wiederholung in Endlosschleife)  `[erledigt]`
**Symptom (real):** Das Modell rief `get_datetime` **hunderte Male pro Antwort** auf → minutenlanger Hänger,
Anfrage nie beantwortet.

**Bereits umgesetzt:**
- `get_datetime`-Tool **entfernt** (war der Hauptablenker; Zeit steht via `_now_hint` im Prompt).
- **`frequency_penalty`** (Default 0.3, Admin-UI) gegen Token-Wiederholung.
- **Loop-Schutz** im Tool-Loop: identische Aufrufe werden entdoppelt + „jetzt antworten"-Nudge; Hartabbruch nur
  bei >15 Calls/Antwort oder >12 unique → dann adaptive Eskalation auf Denken.

**Weitere Lösungen ohne Hardware (falls es wiederkehrt):**
- **Sampling am llama.cpp/Server härten:** `repeat_penalty` ~1.1, `min_p` ~0.05 statt purem Greedy —
  bricht Wiederholungs-Attraktoren.
- Tool-Anzahl senken (Punkt 1) reduziert Degenerations-Anfälligkeit zusätzlich.

---

## 4. 🔴 GPU-Server: 502 / Kaltstarts durch Modell-Swapping (llama-swap)  `[teilweise]`
**Symptom (real, mehrfach):** `502 Bad Gateway` von `192.168.66.225:8080` mitten in Läufen — typisch, wenn
llama-swap ein Modell erst laden muss.

**Bereits umgesetzt:** `_post_llm` mit **Retry bei transienten 5xx/Verbindungsfehlern**.

**Lösungen ohne Hardware:**
- **Modell-Pinning / weniger Swapping:** EIN Hauptmodell dauerhaft resident halten (kein Wechsel pro Anfrage).
  Das eliminiert die Kaltstart-502 fast vollständig.
- **vLLM/TGI statt häufigem Swap** (siehe Punkt 9) — ein resident geladenes Modell, kein Lade-502.
- **Health-/Warmup-Ping** nach Modellwechsel, bevor echte Requests laufen.

---

## 5. 🟡 Kontext-/Ergebnisgröße sprengt das Fenster  `[teilweise — Token-Budget 2026-06-24]`
> Neu: `context_budget.py` budgetiert den GESAMT-Prompt gegen `llm_ctx` und trimmt+fasst alte Turns
> rollierend zusammen; `fetch_url`/Tool-Ergebnisse zusätzlich kontext-sicher gekappt. Siehe
> `orchestrator/CHANGES-2026-06-24.md` #3.

**Symptom (real):** Ein Skill lieferte die volle Serverliste (~40 000 Zeichen) → früher Abbruch („kein
Ergebnis", stdout-Kappung); große Tool-Ergebnisse blähen den Kontext und verwirren das Modell.

**Bereits umgesetzt:** Skill-Ergebnis über Datei (keine stdout-Kappung) + Rückgabe an die KI auf 6000 Zeichen
begrenzt mit „im Skill filtern"-Hinweis; DPM-Skill auf `pu>0`-Filter umgebaut (kompakt).

**Lösungen ohne Hardware:**
- **Server-seitig filtern/aggregieren** (im Skill/Tool), nicht roh zurückgeben — als Konvention/Prompt-Regel.
- **Paginierung** für lange Listen; **Zusammenfassen-dann-ablegen** (großes Ergebnis ins Workspace/RAG, der KI
  nur Kurzfassung + Verweis geben).
- **Verlauf trimmen/zusammenfassen** bei langen Gesprächen (rollierende Zusammenfassung), spart KV-Cache.

---

## 6. 🟡 Latenz (Denken ~5–8 s, mehrstufige Tool-Loops, begrenzter Durchsatz)  `[offen]`
**Lösungen ohne Hardware:**
- **Hybrid bleibt sinnvoll:** einfache Turns schnell (ohne Denken), nur Agenten-Turns mit Denken (Punkt 2).
- **Tool-Anzahl/Schema-Größe senken** (Punkt 1) → kürzerer Prompt → schneller + billiger.
- **Prompt-Caching** (llama.cpp `cache_prompt`/Prefix-Cache) für den stabilen System-Prompt-Teil.
- **TTS früh streamen** (ist umgesetzt) → gefühlte Latenz sinkt.
- **Kleineres Schnell-Modell** für triviale Turns, großes nur für Agenten-Arbeit (Routing).

---

## 7. 🟡 Modell-Leistungsdecke: gemma-12b ist für werkzeugreiche Agentenarbeit grenzwertig  `[offen]`
**Kernbefund der Sitzung:** Die Infrastruktur ist solide; die Rest-Unzuverlässigkeit ist die **Modellgrenze**.

**Lösung ohne Hardware (größter nachhaltiger Hebel):** **Ein in Tool-Use stärkeres Modell, das auf die
vorhandenen 24 GB passt.** 2×3060 (24 GB) tragen via **Tensor-Parallelismus (vLLM)**:
- **Sweet Spot: ein 14B-Modell in Q5/Q6** (~10–13 GB) → reichlich Platz für KV-Cache/Kontext, spürbar besserer
  Function-Caller als gemma-12b. Konkret prüfen: **Qwen2.5-14B-Instruct** oder **Qwen3-14B** (beide sehr gut im
  Tool-Use).
- **Grenzbereich: 27–32B in Q4** (~16–20 GB) passt in 24 GB, aber **Kontext/KV-Cache wird knapp** → kleineres
  Kontextfenster, langsamer. Nur sinnvoll, wenn Tool-Zuverlässigkeit Vorrang vor Tempo/Kontext hat
  (z.B. das bereits vorhandene gemma4-26b, oder Qwen2.5-32B Q4 mit reduziertem Kontext).
- **vLLM** hält EIN Modell resident (löst nebenbei Punkt 4 – kein Lade-502) und nutzt beide GPUs gemeinsam —
  das ist „die Hardware ausnutzen", keine Erweiterung. (Realistisch nur EIN gutes Modell gleichzeitig in 24 GB.)
- Alternativ: das aktuelle kleine Modell bleibt für Smalltalk, ein stärkeres NUR für Agenten-/Tool-Turns
  (Routing) — aber bei 24 GB konkurrieren zwei Modelle um knappen VRAM, daher eher EIN gutes Modell.

---

## 8. 🟡 „Halluzinierter Erfolg" & selbstbewusste Fehl-Antworten  `[teilweise — Verify-by-Tool 2026-06-24]`
> Neu: Erfolgsbehauptungen ohne geglückten Side-Effect-Tool-Aufruf werden erkannt und als
> `unverified_claim` geloggt/gemessen (`/api/admin/metrics`); Prompt-Regel verschärft. Siehe
> `orchestrator/CHANGES-2026-06-24.md` #10.

**Symptom (real):** Modell behauptete „per Telegram gesendet/Skill erstellt", ohne das Werkzeug zu nutzen;
bzw. „Abfrage fehlgeschlagen, prüfe die Verbindung", obwohl es nur das falsche Tool genommen hatte.

**Bereits umgesetzt:** Prompt-Regeln „handeln statt ankündigen / nie ‚erledigt' ohne Tool-Aufruf"; dediziertes
`send_image`-Tool.

**Lösungen ohne Hardware:**
- **Verify-by-Tool-Muster:** Bei Aktionen mit Außenwirkung das Ergebnis aus dem Tool zitieren lassen.
- Denken (Punkt 2) reduziert beides deutlich.
- Klare Fehlermeldungen der Tools, damit das Modell den Unterschied „kein Tool" vs. „Tool ohne Treffer" erkennt.

---

## 9. 🟡 Architektur: alle Tool-Kategorien sind dauerhaft „an"  `[teilweise — MCP erledigt 2026-06-24]`
> Client-Aktionen & Automationen waren bereits deferred (kompakte Meta-Tools). **MCP** ist seit
> 2026-06-24 ebenfalls deferred (Katalog + `search_mcp_tools`/`load_mcp_tools`), siehe
> `orchestrator/CHANGES-2026-06-24.md` #7. Damit ist der größte Schema-Bloat weg.

Interne Tools + MCP + Client-Aktionen + Skills sind gleichzeitig im Schema. Das verteuert jeden Turn (Tokens)
und verschärft Punkt 1.
**Lösung ohne Hardware:** Das **Deferred-Loading der Skills generalisieren** (Katalog + `search/load`-Meta-
Tools) auf MCP und Client-Aktionen; pro Kanal/Nutzer/Rechtelage nur Relevantes einblenden.

---

## 10. 🟢 Skill-Hygiene: Duplikate, Wildwuchs, veraltete Skills  `[offen]`
**Symptom (real):** In der Registry liegen nahezu doppelte Skills (`get_dpm_systems`, `dpm_get_systems`,
`netzwerk_scan`, `full_network_scan`, `dpm_full_update_single`/`_multi`). Mehr Skills → größerer Katalog →
Punkt 1 verschärft.
**Lösungen ohne Hardware:** Dedupe/Aufräumen im Admin-UI; Namens-/Zweck-Konvention; „ähnliches Skill existiert
bereits?"-Hinweis beim Anlegen; selten genutzte Skills automatisch deaktivieren.

---

## 11. 🟢 Identität im Web-Chat  `[offen]`
Getipptes „ich bin Daniel" wird NICHT als Identität erkannt (nur Stimme) → Skills/Workspace laufen als `guest`.
**Lösungen ohne Hardware:** optionaler Login/PIN im Web-UI, oder getippten Namen mit niedriger Konfidenz +
Bestätigung akzeptieren; Session an einen Nutzer binden.

---

## 12. 🟢 Sicherheit selbst-gebauter/erhöhter Skills (Prompt-Injection)  `[teilweise]`
Jarvis liest Webinhalte und kann sich Skills bauen → manipulierte Inhalte könnten zu bösartigem Code verleiten.
**Bereits umgesetzt:** Sandbox-Isolation, Autonomie-Blacklist, erhöhte Rechte nur Admin-freigeschaltet + nicht
autonom, Code-Änderung resettet Rechte.
**Weiter ohne Hardware:** Review-Pflicht großer/erhöhter Skills beibehalten; Egress der Sandbox einschränken
(nur nötige Ziele); Audit-Sicht im Admin-UI ausbauen.

---

## 13. 🟢 Voice-Satellit: Wake-Word & Dual-Mic  `[offen]`
> Teil-Fortschritt 2026-06-24: **Barge-in (Abbruch)** end-to-end vorhanden (Cancel-Registry +
> `POST /api/chat/cancel` + WS `cancel`; Frontend-Hook). Echtes Streaming-STT/AEC steht noch aus
> (GPU-seitiger Streaming-ASR). Siehe `orchestrator/CHANGES-2026-06-24.md` #12.

Wake-Word reagiert mäßig; 2-Mic-BSS kann das WakeNet-Signal verschlechtern (siehe ESP-Notizen).
**Lösungen ohne Hardware:** Mono testen (`JARVIS_DUAL_MIC 0`), Mic-Gain/Empfindlichkeit justieren (remote),
ggf. anderes Wake-Modell. Barge-In (AEC) nur Mono, da CPU-begrenzt.

---

## Kurzfazit / Priorisierung
1. **Stärkeres (tool-fähiges) Modell auf den vorhandenen 24 GB via vLLM (tensor-parallel), resident** —
   z.B. Qwen2.5-14B Q5/Q6; adressiert auf einen Schlag Punkt 1, 2, 4, 7 und 8 am nachhaltigsten.
   Keine neue Hardware nötig.
2. **Tool-Subsetting / Deferred-Loading auf alle Tool-Kategorien** (Punkt 1/9) — entlastet jedes Modell.
3. **Denken für Agenten-Turns** (Punkt 2) — sofort wirksam, reine Config/Heuristik.
4. Rest sind Feinschliff/Hygiene.
