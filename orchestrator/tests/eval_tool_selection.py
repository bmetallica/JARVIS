#!/usr/bin/env python3
"""
Eval-Harness: Tool-Auswahl-Trefferquote des Agenten-Modells (#1).

Misst objektiv, ob das Modell für typische Äußerungen das RICHTIGE Werkzeug zuerst wählt —
statt sich auf anekdotische Beobachtungen zu verlassen (vgl. problems.md #1/#2). Nutzt denselben
System-Prompt-Aufbau und dieselben (deferred) Tool-Schemas wie der echte Turn.

Lauf:
    python3 tests/eval_tool_selection.py                # think=True (empfohlen für Agenten-Turns)
    THINK=0 python3 tests/eval_tool_selection.py        # ohne Denken (adaptive-Fast-Pass simulieren)
    python3 tests/eval_tool_selection.py --min 0.8      # exit!=0, wenn Trefferquote < 80% (für CI)

Braucht den GPU-/LLM-Server (config.llm_url). Jede Aufgabe = ein echter Modell-Aufruf.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import services
import tools
import skills
import mcp_hub

HERE = os.path.dirname(os.path.abspath(__file__))


def build_system(cfg: dict) -> str:
    """Repräsentativer System-Prompt: Persona + Skill-/MCP-Katalog (wie _prepare_turn, vereinfacht)."""
    sys_p = cfg["system_prompt"]
    routing = ("\n\nWähle für Aktionen das passende Werkzeug. Web-Inhalte: fetch_url. "
               "Eigene Server/Updates: über die Skills (search_skills/run_skill). "
               "Smart-Home/externe Geräte: erst search_mcp_tools/load_mcp_tools. "
               "Aktionen auf dem PC des Nutzers: client_action.")
    return sys_p + routing + skills.catalog_hint() + mcp_hub.catalog_hint()


def first_tool(res: dict):
    tc = res.get("tool_calls") or []
    return tc[0]["name"] if tc else None


def run():
    think = os.environ.get("THINK", "1") != "0"
    min_acc = 0.0
    if "--min" in sys.argv:
        min_acc = float(sys.argv[sys.argv.index("--min") + 1])

    cfg = config.get()
    try:
        import asyncio
        asyncio.get_event_loop().run_until_complete(mcp_hub.refresh())
    except Exception:
        pass

    tasks = json.load(open(os.path.join(HERE, "golden_tasks.json")))["tasks"]
    system = build_system(cfg)
    available = list(tools.TOOL_SCHEMAS)   # MCP ist deferred — nicht direkt eingeblendet

    passed = evaluated = 0
    print(f"=== Tool-Auswahl-Eval  (Modell={cfg['llm_model']}, think={think}) ===\n")
    for t in tasks:
        if t.get("requires") == "mcp" and not mcp_hub.has_servers():
            print(f"  SKIP  {t['message'][:60]!r}  (kein MCP-Server konfiguriert)")
            continue
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": t["message"]}]
        try:
            res = services.llm_call(messages, cfg, available, think=think)
        except Exception as e:
            print(f"  ERR   {t['message'][:50]!r}: {e}")
            evaluated += 1
            continue
        got = first_tool(res)
        evaluated += 1
        if t.get("expect_no_tool"):
            ok = got is None
            want = "(kein Tool)"
        else:
            ok = got in t.get("expect_any_of", [])
            want = "|".join(t.get("expect_any_of", []))
        passed += ok
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {t['message'][:52]!r:54}  erwartet={want:32}  gewählt={got}")

    acc = passed / (evaluated or 1)
    print(f"\nTrefferquote: {passed}/{evaluated} = {acc:.0%}")
    if min_acc and acc < min_acc:
        print(f"FAIL: unter Schwelle {min_acc:.0%}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run())
