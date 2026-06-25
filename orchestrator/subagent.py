"""
Subagenten (Phase 4) — „zero-context-cost"-Delegation.

Eine mehrstufige Teilaufgabe läuft in einem EIGENEN, frischen Kontext (eigener Mini-Prompt + nur die
nötigen Werkzeuge) und liefert dem Hauptlauf nur das ENDERGEBNIS als eine Tool-Antwort zurück — der
40k-Hauptkontext bleibt sauber (passt zum Token-Budget, #3).

- Modellwahl aus der Registry (Rolle `subagent`, Phase 0) — z.B. ein kleineres/schnelleres Modell.
- Limits: Rekursionstiefe (kein endloses Weiter-Spawnen) + Nebenläufigkeit (ein residenter GPU-Slot).
- Rechte/Namespace = die des aufrufenden Nutzers (geerbt aus dem Eltern-ctx); Autonomie-Blacklist gilt weiter.
"""
from __future__ import annotations

import asyncio

import config
import services

_MAX_DEPTH = 1                       # Subagent darf selbst KEINEN Subagenten starten
_SEM = asyncio.Semaphore(2)          # begrenzte Nebenläufigkeit (GPU-Slot-Konkurrenz)
_SUB_SYSTEM = (
    "Du bist ein fokussierter Teil-Agent. Erledige die folgende Teilaufgabe selbstständig mit den "
    "verfügbaren Werkzeugen. Antworte am Ende mit einem KNAPPEN, rein faktischen Ergebnis (keine "
    "Anrede, keine Floskeln) — dieses Ergebnis wird an den Hauptagenten zurückgegeben."
)


def can_spawn(parent_ctx: dict) -> bool:
    return int((parent_ctx or {}).get("subagent_depth", 0)) < _MAX_DEPTH


async def run_subagent(task: str, parent_ctx: dict, max_steps: int = 6) -> str:
    """Teilaufgabe in isoliertem Kontext erledigen; gibt einen knappen Ergebnis-String zurück."""
    import tools                      # lokal → kein Import-Zyklus (tools importiert subagent nur lazy)
    task = (task or "").strip()
    if not task:
        return "Teil-Agent: keine Aufgabe angegeben."

    cfg = dict(parent_ctx.get("cfg") or config.get())
    sub_model = config.model_for("subagent", cfg)
    if sub_model:
        cfg["llm_model"] = sub_model
        cfg["llm_ctx"] = config.ctx_for("subagent", cfg)

    # Frischer Kontext, aber Rechte/Identität des Eltern-Laufs geerbt; eigener Tool-Zustand.
    ctx = {**parent_ctx, "subagent_depth": int(parent_ctx.get("subagent_depth", 0)) + 1,
           "loaded_skills": set(), "loaded_mcp": set(), "turn_tools": [], "turn_tool_calls": [],
           "status_cb": None}
    # Eigenes Spawnen verhindern (kein spawn_subagent im Werkzeugsatz des Subagenten).
    tools_avail = [t for t in tools.TOOL_SCHEMAS if t["function"]["name"] != "spawn_subagent"]

    working = [{"role": "system", "content": _SUB_SYSTEM},
               {"role": "user", "content": task}]
    seen: dict = {}
    async with _SEM:
        for _ in range(max_steps):
            try:
                res = await asyncio.to_thread(services.llm_call, working, cfg, tools_avail, True)
            except Exception as e:
                return f"Teil-Agent fehlgeschlagen (LLM): {e}"
            if res.get("tool_calls"):
                working.append(res["raw"])
                for tc in res["tool_calls"][:5]:
                    key = tc["name"] + "|" + str(tc.get("args"))
                    if key in seen:
                        result = seen[key] + "\n(bereits abgerufen — nutze das Ergebnis.)"
                    else:
                        result = await tools.execute_tool(tc["name"], tc["args"], ctx)
                        seen[key] = result
                    working.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)[:6000]})
                if len(seen) > 10:
                    break
                continue
            return (res.get("content") or "").strip() or "(Teil-Agent: kein Ergebnis)"
    return "(Teil-Agent: Schrittlimit erreicht — Teilergebnis unklar)"
