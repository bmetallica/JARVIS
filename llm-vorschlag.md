# LLM-Vorschlag — beste Konstellation für 22 GB VRAM (Stand Juli 2026)

**Recherchiert:** 2026-07-02 (v2, Nachrecherche) · **Rahmen:** 2× RTX 3060 (24 GB) − Whisper ~2 GB =
**22 GB nutzbar**; **gemma4-12b gepinnt** (für andere Anwendungen) → für den JARVIS-Agenten bleibt ein
Slot von **~12–13 GB**. Anforderungen: Deutsch (Pflicht), Tool-Calling (Hauptschmerz), Sprach-Latenz,
möglichst großer Kontext.

---

## 0. Nachrecherche (v2) — die zuerst übersehenen Kandidaten

| Modell | Daten | Bewertung für JARVIS |
|---|---|---|
| **NVIDIA Nemotron 3 Nano 30B-A3B** (Dez 2025; Omni-Variante 28.04.2026) | 31,6B MoE, **3,2B aktiv**, **Hybrid Mamba-Transformer** (→ winziger KV-Cache, **3,3× schneller als Qwen3-30B-A3B**, 2,2× schneller als gpt-oss-20b), **1M Kontext**, konfigurierbares Reasoning, **Deutsch explizit unterstützt**, llama.cpp/GGUF ✓ (unsloth/bartowski), „läuft auf 24 GB" | ⭐ **stärkster neuer Agent-Kandidat**: Tempo-Monster mit Deutsch. Q4 ≈ 17–18 GB → neben dem Pin nur mit `--n-cpu-moe` (bei 3,2B aktiv praktikabel); ohne Pin-Anpassung die beste „schnell UND schlau"-Option. Kein Vision (außer Omni-Variante → für Sprachassistent perspektivisch spannend: Audio-Input nativ) |
| **Ministral-3-14B** (Mistral, Dez 2025, Apache 2.0) | 14B dense, Varianten instruct + **reasoning** (85 % AIME), EU-Modell (Deutsch traditionell gut) | **passt als einziger neuer Kandidat VOLL in den 13-GB-Slot** (Q4 ≈ 8,5 GB, sogar Q6 ≈ 11,5 GB!) — direkter 1:1-Ersatzkandidat für qwen3-14b, per Eval vergleichen |
| Qwen3.6-35B-A3B („Now Open to All", Juni 2026) | 35B MoE, 3B aktiv, 262K ctx, Top-Agentic (schlägt Qwen3.5-27B) | stark, aber ~21 GB Q4 → neben Pin nur mit großzügigem `--n-cpu-moe`; Deutsch gut |
| Mistral Small 4 (März 2026) | **119B** MoE (6B aktiv), vereint Magistral+Pixtral+Devstral, 256K, Apache 2.0 | trotz „Small": 4×H100-Klasse → **raus** |
| Kimi-K2.6 (Moonshot, Ende Juni) | ~1,1T MoE (32B aktiv), Agentic/Coding | **viel zu groß** |
| GLM-5.2 (Zhipu, 16.06., MIT) / Nemotron 3 Ultra (01.06., 550B) / Cohere North Mini Code (09.06.) | Flagships bzw. Code-Spezialist | zu groß / falscher Fokus |
| gpt-oss-20b (Bestand 2025) | 21B MoE (3,6B aktiv), MXFP4 ≈ 13 GB | passt ~in den Slot; Tool-Use gut, **Deutsch nur mittel** → hinter Nemotron/Ministral |

**Kern-Einsicht v2:** Die interessante 2026er-Klasse ist fast durchweg **MoE mit 3–4B aktiven
Parametern** (Gemma4-26B-A4B, Nemotron-Nano-30B-A3B, Qwen3.6-35B-A3B) — als reine VRAM-Ladung
sprengen alle den 13-GB-Slot, aber alle sind mit llama.cpp `--n-cpu-moe` (Experten teilweise ins
System-RAM) auf 12-GB-Karten fahrbar, weil pro Token nur 3–4B rechnen. Das erweitert die realistische
Auswahl deutlich über die v1-Sicht hinaus.

---

## 1. Was sich seit der letzten Analyse getan hat (Juni/Juli 2026)

| Modell | Größe | Kern-Fakten | Relevanz für uns |
|---|---|---|---|
| **Gemma 4 26B-A4B** (Google, 03.06.2026) | 25,2B MoE, **3,8B aktiv** (8/128 Experten) | **multimodal (Vision inkl. OCR)**, **natives Function-Calling**, 35+ Sprachen, **256K ctx**, Sliding-Window 1024 (→ sehr billiger KV-Cache), `enable_thinking`-Toggle, ~85 tok/s auf Consumer-HW, QAT-GGUF von Google | ⭐ **der interessanteste Neuzugang** — gleiche Familie wie euer Pin |
| Gemma 4 Familie | E2B, E4B, 12B, 26B-A4B, 31B | euer gepinntes gemma4-12b ist Teil davon | 31B dense zu groß |
| **Qwen3.6** (27B dense · 35B-A3B) | 27B / 35B MoE (3B aktiv) | 262K ctx, Top-Coding/Agentic, GGUF verfügbar | **passt beides NICHT neben den Pin** (Q4: ~17 GB / ~21 GB) |
| Qwen3.5-9B | 9B dense | multimodal, 262K ctx, Thinking-Toggle, Q6 = 7,4 GB | Kandidat für den 13-GB-Slot |
| GLM-5.2 (Zhipu, 16.06.) | Flagship, MIT | zu groß für lokal | — |
| Nemotron 3 Ultra (550B) / DiffusionGemma | — | zu groß / experimentell | — |

**GGUF-Größen Gemma 4 26B-A4B (unsloth):** UD-Q4_K_M **16,9 GB** · UD-IQ4_XS **13,6 GB** ·
Q3_K_M **12,7 GB** · Q5_K_M 21,2 GB. Vision-Encoder (~550M) integriert.

---

## 2. Die Konstellationen

### K1 ⭐ Empfehlung: Pin-Upgrade — **Gemma 4 26B-A4B ersetzt gemma4-12b als DAS gepinnte Modell**

> Bricht die wörtliche Vorgabe („12b gepinnt"), erfüllt aber ihren Zweck besser: **ein** dauerhaft
> residenter Gemma für die anderen Anwendungen — nur eine Generation größer/schneller.
> **Voraussetzung: deine anderen Anwendungen akzeptieren den Austausch** (gleiche Familie/API-Stil).

| Rolle | Modell | VRAM |
|---|---|---|
| **Agent + Vision + Subagent + andere Apps** | gemma-4-26B-A4B **UD-Q4_K_M (16,9 GB)**, gepinnt | ~18–19 GB inkl. KV (SWA-KV ist winzig) |
| Whisper | unverändert | 2 GB |
| frei für Swap (ornith-9b, Draft …) | | ~2–3 GB |

**Warum das die beste Option ist:**
- **Qualität:** 26B-Kapazität > qwen3-14b UND > gemma4-12b — ein Upgrade für JARVIS *und* die anderen Apps.
- **Tempo:** nur 3,8B aktive Parameter → **schneller als euer heutiges 14B-dense** (Denk-Runden werden billig — genau unser Latenz-Engpass); SWA-1024 macht den KV-Cache selbst bei 128K+ Kontext klein → **großer Kontext ohne VRAM-Schmerz** (deine Priorität!).
- **Stabilität:** natives Function-Calling (Gemma 4 — die alte Gemma-3-Schwäche aus problems.md #7 ist adressiert), `enable_thinking`-Toggle ist mit unserem `_thinking_kwargs`-Code kompatibel.
- **Architektur:** EIN Modell für alles → **kein Swap mehr, keine 502-Kaltstarts**, kein Keepalive nötig.
- **Deutsch:** Gemma-Familie ist traditionell die stärkste offene Familie für Deutsch.

**Risiken/Auflagen (ehrlich):**
1. **llama.cpp aktuell bauen** — Gemma-4-Tool-Calling braucht `--jinja` + die Template-/Tokenizer-Fixes
   (PR #21326/#21343, ~April 2026 gemerged). In Ollama ist es kaputt; llama.cpp aktuell = ok.
   Start: `--jinja --chat-template-kwargs '{"enable_thinking":false}'` als Default (JARVIS toggelt pro Turn).
2. **Erst messen, dann umziehen:** `tests/eval_tool_selection.py` + Golden-Tasks gegen 26B-A4B laufen
   lassen, BEVOR der Pin getauscht wird (Deutsch-/Tool-Verhalten verifizieren, nicht glauben).
3. Sampling laut Doku: temp 1.0 / top_p 0.95 / top_k 64 (unser `llm_tool_temperature=0.2` für
   Tool-Runden bleibt sinnvoll — im Eval gegenprüfen).

### K2 — Falls der Pin zwingend gemma4-**12b** bleiben muss: Qwen3.5-9B + Draft im 13-GB-Slot

| Rolle | Modell | VRAM |
|---|---|---|
| Pin (andere Apps + JARVIS-Vision/Subagent) | gemma4-12b (ctx 16k, q8-KV) | ~8,5–9 GB |
| **Agent** | **Qwen3.5-9B Q6_K (7,4 GB)** + Draft qwen3-0.6b (0,7 GB) | ~10–11 GB inkl. großem q8-KV |
| Whisper | int8_float16 | ~1,2 GB |

- 2026er-Generation, multimodal, 262K ctx, Thinking-Toggle; **Q6 statt Q4** (weniger Tool-Rauschen)
  + **Speculative Decoding** → schnellste Variante. Riesiger Kontext möglich (deine Priorität).
- Ehrlich: 9B < 14B an Roh-Kapazität — die Retry-/Scaffolding-Netze gleichen viel aus, aber
  K1 ist qualitativ klar überlegen. Vorher Eval-Vergleich 9B vs. qwen3-14b fahren.

### K2b — 26B-A4B trotzdem im 13-GB-Slot (MoE-CPU-Offload)

UD-IQ4_XS (13,6 GB) passt nicht ganz → mit llama.cpp **`--n-cpu-moe N`** einen Teil der
Experten-Tensoren ins System-RAM legen (bei nur 3,8B aktiv bleibt das brauchbar schnell, PCIe-
Overhead einkalkulieren) — oder Q3_K_M (12,7 GB) ganz im VRAM (Q3-Qualitätsrisiko fürs
Tool-Calling). **Nur 2. Wahl** — funktioniert, aber K1 ist sauberer. Praktisch als **risikofreier
Testpfad**: 26B-A4B so evaluieren, OHNE den Pin anzufassen; überzeugt es, auf K1 wechseln.

### K3 — der Vollständigkeit halber
- **Qwen3.6-35B-A3B** wäre das stärkste offene Agentic-Modell dieser Klasse (schlägt Qwen3.5-27B
  bei 3B aktiv) — braucht aber ~21 GB → **nur ohne jeden Pin** denkbar. Nicht kompatibel mit deiner Vorgabe.
- Qwen3.6-27B dense (~17 GB): passt ebenfalls nicht neben den Pin.
- Ornith bleibt Coding-Spezialist (kein Deutsch, Zwangs-Reasoning) — nicht für JARVIS.

---

## 3. Empfehlung & Fahrplan (v2 — mit den Nachrecherche-Funden)

**Shortlist für den Eval-Shootout (in dieser Reihenfolge testen):**

| Rang | Kandidat | Rolle/Konstellation | Warum |
|---|---|---|---|
| 1 | **Gemma 4 26B-A4B** | K1: Pin-Upgrade, EIN Modell für alles | Vision integriert + Deutsch top + Function-Calling nativ + kein Swap — ganzheitlich die sauberste Lösung |
| 2 | **Nemotron 3 Nano 30B-A3B** | Agent im Swap-Slot (Q4 + `--n-cpu-moe`), Pin bleibt 12b | schnellstes Modell der Klasse (Mamba-Hybrid), Deutsch explizit, 1M ctx, Reasoning konfigurierbar — macht die teuren Denk-Runden fast gratis; kein Vision (macht das gepinnte gemma) |
| 3 | **Ministral-3-14B** (instruct oder reasoning) | Agent im Slot, **voll im VRAM** (sogar Q6!) | risikoärmster Tausch: gleiche Größe wie heute, aber 2026er EU-Modell in Q6 statt Q4 — kein MoE-Offload, keine Template-Abenteuer |
| 4 | Qwen3.5-9B Q6 + Draft | K2 (Kontext-/Tempo-Fokus) | wie v1 beschrieben |
| 5 | Qwen3.6-35B-A3B | nur mit viel `--n-cpu-moe` oder ohne Pin | stärkstes Agentic, aber am unbequemsten im Budget |

**Fahrplan:**
1. **llama.cpp aktuell bauen** (Gemma-4-Template-Fixes + Nemotron-MoE-Support sind beide neu).
2. Kandidaten 1–3 nacheinander als **Swap-Modelle laden (Pin unangetastet)** und je
   `tests/eval_tool_selection.py` + Golden-Tasks + 3 Alltags-Turns fahren
   (Tool-Trefferquote, Deutsch-Qualität, `turn_done.ms`, VRAM per `nvidia-smi`).
3. Entscheid nach Daten: Gewinner wird Agent (bzw. bei Gemma-Sieg: Pin-Upgrade K1);
   qwen3-14b bleibt als Fallback in der Swap-Gruppe.

**Meine Einschätzung vorab (durch den Eval zu bestätigen):** Für *dein* Profil — Deutsch, Voice-Latenz,
Tool-Zuverlässigkeit, gepinntes gemma — ist das realistische Duell **Gemma 4 26B-A4B (K1)** gegen
**Nemotron 3 Nano 30B-A3B (Agent-Slot)**. Ministral-3-14B ist der solide, langweilige Plan B ohne
jedes Infrastruktur-Risiko.

---

**Quellen:** [HF: gemma-4-26B-A4B-it](https://huggingface.co/google/gemma-4-26B-A4B-it) ·
[unsloth 26B-A4B-GGUF](https://huggingface.co/unsloth/gemma-4-26B-A4B-it-GGUF) ·
[Unsloth: Gemma 4 lokal](https://unsloth.ai/docs/models/gemma-4) ·
[LM Studio: Gemma 4](https://lmstudio.ai/models/gemma-4) ·
[Gemma-4-Template-Fix-Diskussion](https://huggingface.co/google/gemma-4-26B-A4B-it/discussions/20) ·
[OpenCode+Gemma4 Tool-Calling-Gist](https://gist.github.com/daniel-farina/87dc1c394b94e45bb700d27e9ea03193) ·
[Qwen3.6-27B](https://qwen.ai/blog?id=qwen3.6-27b) · [Qwen3.6-35B-A3B](https://qwen.ai/blog?id=qwen3.6-35b-a3b) ·
[Unsloth: Qwen3.6](https://unsloth.ai/docs/models/qwen3.6) ·
[Best local LLMs 2026 (HF-Blog)](https://huggingface.co/blog/daya-shankar/open-source-llm-models-to-run-locally) ·
[Function-Calling-Vergleich Qwen3.6/Gemma4](https://insiderllm.com/guides/function-calling-local-llms/) ·
[LLM-News Juli 2026](https://llm-stats.com/ai-news) ·
**v2:** [Nemotron-3-Nano-30B-A3B Modelcard](https://build.nvidia.com/nvidia/nemotron-3-nano-30b-a3b/modelcard) ·
[unsloth Nemotron-GGUF](https://huggingface.co/unsloth/Nemotron-3-Nano-30B-A3B-GGUF) ·
[Nemotron-3-Guide](https://toolhalla.ai/blog/nvidia-nemotron-3-complete-guide-2026) ·
[Nemotron vs gpt-oss-20b](https://deepinfra.com/blog/nemotron-3-nano-vs-gpt-oss-20b-deepinfra-performance) ·
[Mistral-2026-Guide (Ministral 3 / Small 4)](https://serenitiesai.com/articles/mistral-ai-models-2026-complete-guide) ·
[Mistral Changelog](https://docs.mistral.ai/resources/changelogs) ·
[AINews: Nemotron 3 Familie](https://news.smol.ai/issues/25-12-15-nemotron-3/)
