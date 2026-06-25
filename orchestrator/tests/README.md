# Tests & Eval (Orchestrator)

Zwei Ebenen, bewusst getrennt nach Abhängigkeiten:

## 1. Unit-Tests — schnell, ohne Modell/DB
```bash
python3 tests/run_unit.py        # eigener Mini-Runner, exit!=0 bei Fehler
pytest tests/run_unit.py         # falls pytest installiert
```
Decken die in der Juni-2026-Session gebaute Logik ab:
- **#3 Kontext-Budget** (`context_budget.fit`): Trimmen der ältesten Turns + rollierende Zusammenfassung.
- **#4 Observability** (`debug.metrics`): Zählung von Turns/Tools/Fehlern/502.
- **#7 MCP Deferred-Loading** (`mcp_hub`): Katalog, Suche, `schemas_for` nur für geladene Tools.

## 2. Eval-Harness — Tool-Auswahl-Trefferquote (braucht LLM-Server)
```bash
python3 tests/eval_tool_selection.py            # think=True (empfohlen)
THINK=0 python3 tests/eval_tool_selection.py    # adaptive-Fast-Pass (ohne Denken)
python3 tests/eval_tool_selection.py --min 0.8  # exit!=0 bei <80% (CI-Gate)
```
Misst objektiv, ob das Modell für typische Äußerungen das **richtige Werkzeug zuerst** wählt
(adressiert `problems.md` #1/#2: falsches Tool / halluzinierter Erfolg). Die Aufgaben stehen in
`golden_tasks.json` — **bei neuen Tools/Skills dort ergänzen**, dann sind Regressionen messbar.

Empfohlener Einsatz: vor/nach Modellwechsel (gemma↔qwen), Prompt-Änderungen oder Tool-Umbauten
einmal laufen lassen und die Trefferquote vergleichen.
