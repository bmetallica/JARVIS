# Roadmap — Agenten-Evolution (Adaption aus Hermes Agent)

> Stand: 2026-06-24. Integriert die sinnvollen Ideen aus [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
> in JARVIS. Baut auf der Infrastruktur dieser Session auf (persistenter Verlauf, Observability,
> Kontext-Budget, MCP-Deferred, Eval-Harness — siehe `orchestrator/CHANGES-2026-06-24.md`).
>
> Reihenfolge nach Abhängigkeit. Aufwand: **S** (≤1 Tag) · **M** (2–4 Tage) · **L** (>1 Woche).
> Punkt-Nummern beziehen sich auf die Empfehlungsliste (Hermes-Adaption).
>
> **STATUS 2026-06-24: Phase 0–4 sind IMPLEMENTIERT & getestet** — siehe
> [`orchestrator/CHANGES-2026-06-24.md`](./orchestrator/CHANGES-2026-06-24.md) „Teil 3". Offen bleibt nur
> der Langfrist-/Forschungs-Track (Punkt 1, eigenes Tool-Calling-Modell), der separat angegangen wird.

---

## Phase 0 — Modell-Registry + Modellwahl im Admin-UI (Fundament)   **M**

**Warum zuerst:** Mehrere Phasen profitieren davon, das Agenten-Modell (und für Subagenten ggf. ein
zweites, kleineres Modell) **im Admin-UI** wählen zu können — statt `config.json` von Hand zu pflegen.
Außerdem koppelt es die **Slot-/Kontext-Logik**, die wir beim llama-swap-Fix entdeckt haben
(`--parallel N` teilt `ctx` in N Slots → effektiver Kontext = `ctx_total / slots`), sauber an das
Token-Budget (#3).

**Ziel:** Im Admin-UI eine **Modell-Registry** mit Rollen-Zuweisung:
| Feld | Beispiel |
|------|----------|
| `id` | `gemma4-12b`, `qwen3-14b` (aus llama-swap `/v1/models`) |
| `role` | `agent` \| `vision` \| `fast` \| `subagent` |
| `ctx_total` | 49152 |
| `slots` | 2 |
| → `ctx_effective` (berechnet) | 24576 |

Wählt der Admin „gemma4-12b als **agent**, 2 Slots, ctx 49152", setzt der Orchestrator automatisch
`llm_model=gemma4-12b` und `llm_ctx=24576` (= 49152/2) — das Kontext-Budget (#3) und die Denk-Heuristik
adaptieren sofort.

**Design / Dateien:**
- `config.py`: neue Struktur `models: [{id, role, ctx_total, slots}]`; Helfer `model_for(role)` und
  `ctx_for(role)` (= `ctx_total // max(1,slots)`).
- `app.py`: `_prepare_turn` nutzt `config.model_for("agent")` / `config.ctx_for("agent")` statt fixer Keys;
  Endpunkte `GET/POST /api/admin/models` (Liste aus llama-swap `/v1/models` + `/running` anreichern,
  Rollen/Slots speichern).
- `static/admin.html`/`admin.js`: neuer Tab „Modelle" — Dropdown je Rolle, Slot-Eingabe, berechneter
  effektiver Kontext, „Speichern".
- `services.py`: `llm_call`/`llm_stream` bekommen optional `model`/`ctx` (für Subagent-Rolle, Phase 4).

**Grenze (ehrlich):** Der Orchestrator kann das **tatsächliche** `--parallel` auf dem GPU-Server
(192.168.66.225, llama-swap) nicht selbst umstellen — das bleibt eine manuelle Änderung dort. Die Registry
speichert die *bekannten* Werte (`ctx_total`, `slots`) und treibt damit das Budget. *Stretch:* falls
llama-swap später eine Management-API bekommt, kann der UI-Knopf den Reload anstoßen.

**Akzeptanz:** Agent-Modell im UI auf gemma4-12b/2 Slots umstellen → `/api/admin/metrics` zeigt Turns mit
dem neuen Modell, Budget rechnet mit 24576, kein Code-/JSON-Eingriff nötig.

---

## Phase 1 — Cross-Session-Recall (Punkt 2)   **S–M**

**Idee (Hermes):** „FTS5 session search with LLM summarization for cross-session recall." → JARVIS soll
sich an **frühere Gespräche** erinnern („Was hatten wir letzte Woche zu X?").

**Voraussetzung schon erfüllt:** `chat_history` ist seit dieser Session persistent (#2) — die Daten liegen,
ungenutzt für Recall.

**Design / Dateien:**
- **Indexierung:** Bei `turn_done` den Nutzer+Assistent-Wortwechsel embedden (vorhandenes
  `services.embed` / nomic-embed) und in `store.add(kind="conversation", namespace=u<uid>, source=sid, …)`
  ablegen — der pgvector-Store kann das schon (nur neues `kind`).
- **Abruf:** neues Tool `recall_conversation(query)` in `tools.py` → `store.search(kind="conversation", …)`
  Top-k, dann **LLM-Kurzzusammenfassung** der Treffer (ein billiger `llm_call`). Optional automatischer
  Recall in `_prepare_turn` (wie `recall_memory`, mit `min_score`-Schwelle), in den System-Prompt.
- Alternativ/zusätzlich Postgres-FTS (`tsvector`/GIN) für wörtliche Suche — Embeddings reichen aber meist.

**Risiken:** Datenschutz (alte Gespräche durchsuchbar) → an `log_redact`/Namespace-Grenzen halten;
Embedding-Kosten pro Turn (gering, nomic ist günstig).

**Akzeptanz:** „Worüber haben wir neulich bzgl. DPM gesprochen?" liefert den relevanten alten Wortwechsel
zusammengefasst.

---

## Phase 2 — Selbst-verbessernde Skills (Punkt 3)   **M**

**Idee (Hermes):** „skills self-improve during use." → kaputte/fehleranfällige Skills reparieren sich.

**Voraussetzung schon erfüllt:** `skills.py` trackt bereits `run_count`, `fail_count`, `last_error`, `version`.
Es fehlt nur die Reparatur-Schleife.

**Design / Dateien:**
- `skills.py`: Helfer `unhealthy(threshold)` → Skills mit `fail_count ≥ N`. Versions-Historie behalten
  (Rollback-Möglichkeit).
- **Reparatur-Lauf** (zwei Modi, konfigurierbar):
  1. **Vorschlag:** Admin-UI-Badge „⚠ instabil" + Knopf „reparieren lassen" → JARVIS macht
     `describe_skill` → analysiert `last_error` → `update_skill(code=…, test_args=…)` mit Pflicht-Test.
  2. **Autonom:** ein eingebauter `automations.py`-Job scannt periodisch, repariert und **meldet dem
     Besitzer** (Rückkanal/Telegram). Guardrails: nur Sandbox-Skills automatisch (erhöhte nur mit
     Admin-Freigabe), max. Versuche begrenzen, bei wiederholtem Misserfolg deaktivieren statt endlos.
- `static/admin.*`: „Skill-Gesundheit" (Läufe/Fehler/letzter Fehler/Version) im Skills-Tab.

**Risiken:** Auto-Änderung von Code → strenger Pflicht-Test vor Übernahme (existiert in `update_skill`),
Versionierung für Rollback, erhöhte Skills nie autonom.

**Akzeptanz:** Ein absichtlich kaputtes Sandbox-Skill wird nach N Fehlläufen automatisch repariert (oder
klar geflaggt) und der Besitzer informiert.

---

## Phase 3 — Agent-kuratiertes Nutzermodell (Punkt 4)   **M**

**Idee (Hermes):** SOUL.md/USER.md — ein **kohärentes, fortgeschriebenes Profil** pro Nutzer statt nur
verstreuter Einzelfakten.

**Abgrenzung:** JARVIS hat schon atomare pgvector-Fakten (`save_memory`/`recall_memory`). Das Nutzermodell
ist die **Zusammenfassungs-Schicht** darüber: ein lesbarer Steckbrief (Vorlieben, Rollen, Projekte, Tonfall),
den der Agent pflegt.

**Design / Dateien:**
- **Speicher:** neue Tabelle `user_profile(user_id, content, updated_at)` in `store.py` (oder
  `kind="profile"` als Einzeldokument je Namespace).
- **Pflege:** nach Gesprächen / periodisch (wie die rollierende Summary aus #3, aber persistent je Nutzer)
  ein `llm_call`, der **neue Erkenntnisse in das bestehende Profil einarbeitet** (merge, nicht anhängen),
  gekappt auf z.B. 2–3k Zeichen.
- **Nutzung:** `_prepare_turn` blendet das Profil des erkannten Sprechers in den System-Prompt ein
  (ersetzt/ergänzt die heutige lose Fakten-Liste).
- **Admin-UI:** Profil je Nutzer ansehen/editieren/leeren (Transparenz + Kontrolle).

**Risiken:** Falsche Schlüsse im Profil → editierbar + an Stimm-Identität gebunden (nicht an Gast-Turns);
Datenschutz wie Phase 1.

**Akzeptanz:** Über mehrere Sitzungen wächst ein sinnvolles Profil; im Admin-UI sicht- und korrigierbar;
Antworten werden spürbar persönlicher.

---

## Phase 4 — Subagenten mit „zero-context-cost" (Punkt 5)   **L**

**Idee (Hermes):** „spawn isolated subagents… collapsing multi-step pipelines into zero-context-cost turns."
→ Eine mehrstufige Teilaufgabe läuft in einem **eigenen, frischen Kontext** und liefert dem Hauptlauf nur
das **Endergebnis** zurück — der 40k-Hauptkontext bleibt sauber (passt zu Budget #3).

**Design / Dateien:**
- neues `subagent.py` (oder `_run_loop` mit Sub-Kontext): `run_subagent(task, tool_subset=None, model=None,
  max_steps=6)` → eigener Mini-System-Prompt + nur relevante Tools, eigener Tool-Loop, gibt einen knappen
  Ergebnis-String zurück.
- `tools.py`: Tool `spawn_subagent(task, scope)` — der Hauptagent delegiert (z.B. „recherchiere X gründlich");
  das Sub-Ergebnis kommt als **eine** Tool-Antwort zurück (statt 5 Tool-Runden im Hauptkontext).
- **Modellwahl aus Phase 0:** Subagent kann ein **kleineres/schnelleres** Modell nutzen (`role=subagent`).
- **Sicherheit/Limits:** Rekursionstiefe begrenzen (Subagent darf nicht endlos weiter-spawnen),
  Nebenläufigkeit per Semaphore (ein residenter GPU-Slot!), Rechte = die des aufrufenden Nutzers,
  Autonomie-Blacklist gilt weiter.

**Risiken:** Latenz (zweiter Agenten-Lauf), VRAM-/Slot-Konkurrenz (Phase 0 koppelt Slots), Komplexität.
Deshalb zuletzt.

**Akzeptanz:** „Recherchiere gründlich X und nenne mir 3 Kernpunkte" → Subagent macht intern mehrere
Schritte, Hauptverlauf enthält nur die 3 Kernpunkte; Hauptkontext bleibt klein.

---

## Langfrist-/Forschungs-Track — eigenes Tool-Calling-Modell (Punkt 1)   **L+**

Nicht „jetzt", sondern sobald genug echte Nutzungsdaten vorliegen. Bausteine:
1. **Daten:** JSONL-Trajektorien (#4) ✅ schon vorhanden.
2. **Filtern/Labeln:** nur Turns ohne Fehler/`unverified_claim`, idealerweise eval-bestätigte Tool-Wahl
   (#1/#10) 🟡 teils vorhanden.
3. **Exporter (neu):** Skript `tools→training`: Logs → `{messages, tools, expected tool_calls}`-Paare
   (OpenAI-Function-Format), dedupliziert.
4. **Fine-Tune (neu):** LoRA von qwen3-14b (z.B. unsloth/axolotl) auf den 2×3060 oder gemieteter GPU.
5. **Servieren:** Adapter über llama-swap als zusätzliches Modell, in der Registry (Phase 0) als `agent`
   wählbar → A/B gegen das Basismodell per Eval-Harness (#1).

**Nutzen:** zuverlässigere Tool-Wahl **ohne Denken** → schneller + stabiler; der nachhaltigste Hebel gegen
problems.md #1/#2/#7. **Voraussetzung:** mehrere hundert bis tausend gute Trajektorien.

---

## Empfohlene Reihenfolge
1. **Phase 0** (Modell-Registry) — Fundament, schaltet Modellwahl + Slots frei (dein Wunsch).
2. **Phase 1** (Cross-Session-Recall) — schneller, sichtbarer Gewinn auf bereits gebauter `chat_history`.
3. **Phase 2** (Selbst-verbessernde Skills) — nutzt schon erfasste Fehlerdaten.
4. **Phase 3** (Nutzermodell) — baut auf der Summary-Infrastruktur auf.
5. **Phase 4** (Subagenten) — größter Brocken, nutzt Phase 0.
6. **Punkt 1** (eigenes Modell) — Langfrist, sobald Daten reichen.
