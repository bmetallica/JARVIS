# GPU-Server — Optimierungsvorschläge (llama.cpp / llama-swap)

**Stand:** 2026-07-02 · **Host:** 192.168.66.225 (2× RTX 3060 = 24 GB) · **Randbedingungen:**
Whisper-STT dauerhaft (~2 GB) · **gemma4-12b fest gepinnt** (für andere Anwendungen) · Rest für den
JARVIS-Agenten (qwen3-14b) + swap-bare Modelle (ornith).
Ziel: die kleinen Modelle **stabiler** (Tool-Calls) und **schneller** (Prefill + Decode) — ohne neue Hardware.

> Orchestrator-seitige Maßnahmen (Prompt-Cache-Ordnung, Tool-Temperatur, Router …) stehen in
> [`erweiterungen.md`](./erweiterungen.md) Abschnitt 11 — beide Listen greifen ineinander.

---

## 0. VRAM-Budget (die Rechnung, an der alles hängt)

| Posten | VRAM (geschätzt) | Anmerkung |
|---|---|---|
| Whisper large-v3-turbo (CT2, fp16) | ~2,0 GB | auf GPU1; mit `int8_float16` → **~1,2 GB** (Maßnahme 6) |
| gemma4-12b Q4_K_M gepinnt | ~7,5 GB Gewichte + KV | KV @32k f16 ≈ 3 GB(!) → mit 16k + q8-KV ≈ **~8,5–9 GB gesamt** |
| qwen3-14b Q4_K_M | ~8,5 GB Gewichte | KV @32k f16 ≈ 5 GB / **q8 ≈ 2,6 GB** / q8@24k ≈ 2 GB |
| Draft-Modell qwen3-0.6b (Maßnahme 2) | ~0,7 GB | |
| Compute-Buffer/Fragmentierung | ~1–1,5 GB | pro Karte einplanen |

**Konsequenz:** Mit gepinntem gemma bleiben für den Agenten real **~12–13 GB**. Das reicht für
qwen3-14b **nur mit Flash-Attention + q8-KV** komfortabel — und es erzwingt eine ehrliche Entscheidung
bei Maßnahme 2 vs. 3 (beides zusammen passt nicht).

⚠️ **Konflikt, bewusst entscheiden:** `ornith-35b` (IQ4_XS, 18,8 GB) **passt NICHT neben das gepinnte
gemma** (13 GB frei). Mit Pin bleibt nur `ornith-9b` (6,5 GB) im Swap-Slot nutzbar. Wer ornith-35b
will, muss den gemma-Pin für diese Zeit lösen (llama-swap-Gruppe statt Pin).

---

## 1. 🔴 Flash-Attention + KV-Cache-Quantisierung — *zuerst, macht alles andere möglich*

```
# beide Modelle (qwen UND gemma):
-fa  --cache-type-k q8_0  --cache-type-v q8_0
```
- Halbiert den KV-Speicher (qwen @32k: 5 GB → 2,6 GB; gemma analog) bei minimalem Qualitätsverlust.
- `--cache-type-v` erfordert Flash-Attention (`-fa`).
- **Gewinn:** ~3–4 GB frei → Raum für Draft-Modell ODER bessere Quantisierung ODER mehr Kontext.
- Für das gepinnte gemma zusätzlich: **Kontext auf 16k senken** (Vision-Q&A/Subagent brauchen keine 32k)
  → weitere ~1,5 GB. Im JARVIS-Admin (Modell-Registry) `ctx_total` entsprechend nachziehen.

## 2. 🔴 Speculative Decoding (Draft-Modell) — *größter Decode-Hebel, verlustfrei*

```
# qwen3-14b-Eintrag in llama-swap:
--model-draft /pfad/qwen3-0.6b-q8_0.gguf  --gpu-layers-draft 99  --draft-max 16  --draft-p-min 0.75
```
- Das 0.6b-Modell (gleiche Qwen3-Tokenizer-Familie — Pflicht!) entwirft Tokens, das 14B verifiziert nur:
  auf den 3060ern realistisch **1,5–2,5× schnellere Generierung, mathematisch identische Ausgaben**.
- Kostet ~0,7–1 GB — passt NUR dank Maßnahme 1.
- Besonders wirksam bei den langen Denk-Phasen (Reasoning-Tokens sind gut vorhersagbar).

## 3. 🟡 ALTERNATIV zu 2: Quantisierung anheben (Q4_K_M → Q5_K_M)

- Tool-Calling leidet überproportional unter Q4-Rauschen (falsche Argument-Namen, Format-Ausrutscher —
  exakt die beobachteten Symptome). **Q5_K_M (~10,3 GB)** ist der billigste Stabilitäts-Hebel.
- Passt neben gepinntem gemma nur mit q8-KV und ctx ≤ 24k — und **nicht zusammen mit dem Draft-Modell**.
- **Empfehlung:** erst **2** (Tempo; Stabilität liefert das Orchestrator-Scaffolding), Q5 nur testen,
  wenn der Eval-Harness (`tests/eval_tool_selection.py`) danach noch Tool-Fehlgriffe zeigt. Q6_K (12,4 GB)
  passt neben dem Pin definitiv nicht.

## 4. 🟡 Prefix-Cache serverseitig ausreizen

```
--cache-reuse 256
```
- Erlaubt llama-server, übereinstimmende Prompt-Präfixe auch bei kleinen Abweichungen wiederzuverwenden.
- Entfaltet die volle Wirkung erst zusammen mit dem Orchestrator-Umbau (volatile Prompt-Teile ans Ende,
  erweiterungen.md 11.1) — dann wird der große Katalog-/Persona-Block nicht mehr jeden Turn neu ge-prefillt
  (auf 3060ern mehrere Sekunden pro Turn).

## 5. 🟡 Platzierung & Batches (Feintuning)

```
# qwen3-14b: Whisper-GPU schonen (Startwerte, mit nvidia-smi feinjustieren):
--tensor-split 30,70  --main-gpu 1
# gemma gepinnt möglichst komplett auf GPU0.
# Prefill-Tempo (VRAM-abhängig testen):
-ub 1024   (Default 512; nur wenn Budget nach 1/2 noch Luft hat)
```
- Ziel: GPU0 ≈ gemma (9) + qwen-Anteil (~2,5) · GPU1 ≈ Whisper (2) + qwen-Anteil (~8,5) + Draft (0,7).
- Nach jedem Schritt `nvidia-smi` prüfen — die Schätzungen oben sind Startwerte, keine Garantien.

## 6. 🟢 Whisper verkleinern

- faster-whisper-Server: `compute_type=int8_float16` → ~2 GB → **~1,2 GB**, kaum messbarer
  Qualitätsverlust bei large-v3-turbo. Schafft genau die Luft, die Maßnahme 2+3 knapp machen.
  (docker-compose: Umgebungsvariable des Containers, z. B. `WHISPER__COMPUTE_TYPE=int8_float16`.)

## 7. 🟢 llama-swap: gemma-Pin sauber abbilden

```yaml
# Skizze (Syntax je nach llama-swap-Version):
groups:
  pinned:
    persistent: true          # nie entladen
    members: ["gemma4-12b"]
  agent:
    swap: true                # qwen3-14b <-> ornith-9b teilen sich den Rest-Slot
    members: ["qwen3-14b", "ornith-9b"]
# ornith-35b NICHT in "agent" aufnehmen, solange der Pin aktiv ist (OOM, s.o.)
```

## 8. 🟢 Kleinigkeiten

- `--mlock` für das gepinnte gemma (kein Paging), `--no-mmap` testen für schnelleres Swap-in der agent-Gruppe.
- llama-swap-`ttl` für die agent-Gruppe großzügig (Modell nicht nach Minuten entladen); der
  Orchestrator-Keepalive (`llm_keepalive_s`, Default aus) kann das ergänzen.
- `--parallel 1` beibehalten (jeder Slot teilt den Kontext — Registry geht von 1 aus).

---

## Empfohlene Reihenfolge & Messung

1. **Maßnahme 1** (FA + q8-KV, gemma-ctx 16k) → `nvidia-smi`-Bilanz notieren.
2. **Maßnahme 2** (Draft) → Tempo messen (Turn-Zeiten im Debug-Log `turn_done.ms`).
3. **Maßnahme 4 + 6 + 5** in beliebiger Reihenfolge.
4. Nur bei weiter auffälligen Tool-Fehlgriffen: **Maßnahme 3** statt 2 testen.

**Jede Stufe objektiv messen:** `python3 tests/eval_tool_selection.py` (Trefferquote) + Turn-Latenzen
aus `logs/events-*.jsonl` (`turn_done.ms`, `prompt_tokens`) vorher/nachher vergleichen — nicht nach Gefühl.

**Erwartung kombiniert (1+2+4+6 + Orchestrator-Teil):** Alltags-Turns Richtung **3–6 s**, Schalten
~6–11 s, spürbar weniger No-Think-Fehlgriffe — ohne Modellwechsel und mit unangetastetem gemma-Pin.
