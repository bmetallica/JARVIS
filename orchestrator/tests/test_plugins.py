#!/usr/bin/env python3
"""
Tests für das Plugin-Gateway (Registry, API-Keys, Scopes, KV-Store, Event-Bus).

Braucht Postgres (store.DSN) — KEIN LLM/GPU nötig.
Lauf:  python3 tests/test_plugins.py        (eigener Mini-Runner, exit!=0 bei Fehler)

Deckt ab: pluginsystem.md Kap. 4 (Auth/Scopes), 5.7 (Storage), 6 (Event-Bus),
7/10 (Registry/Schema). Die HTTP-/WS-Schicht wird separat als Live-Smoke getestet.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugins_registry as R
import plugin_bus
import automations

PID = "ci_testplug"
_failures = []


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        _failures.append(name)


def test_registry_and_keys():
    R.init()
    R.delete(PID)
    p = R.register({"id": PID, "name": "CI Plugin", "version": "1.0.0", "type": "external"})
    check("register liefert aktiviertes Plugin", p["enabled"] and p["id"] == PID)

    tok = R.create_key(PID, ["api:llm", "api:storage"], label="ci")
    check("Token-Format jvp_<id>_…", tok.startswith(f"jvp_{PID}_"))

    ki = R.verify_key(tok)
    check("verify_key erkennt gültigen Key", bool(ki) and ki["plugin_id"] == PID)
    check("Scope vorhanden", R.has_scope(ki, "api:llm"))
    check("Scope fehlt → False", not R.has_scope(ki, "api:vision"))
    check("falscher Token → None", R.verify_key(f"jvp_{PID}_falsch") is None)
    check("leerer Token → None", R.verify_key(None) is None)

    R.set_enabled(PID, False)
    check("deaktiviertes Plugin → Key ungültig", R.verify_key(tok) is None)
    R.set_enabled(PID, True)
    check("reaktiviert → Key wieder gültig", R.verify_key(tok) is not None)

    kid = R.list_keys(PID)[0]["kid"]
    R.set_key_scopes(kid, ["api:llm", "api:storage", "api:notify"])
    check("Scopes aktualisiert", "api:notify" in R.verify_key(tok)["scopes"])
    R.revoke_key(kid)
    check("widerrufener Key → None", R.verify_key(tok) is None)


def test_kv_store():
    ns = R.kv_ns(PID, 7, "shared")
    check("user-NS ≠ shared-NS", R.kv_ns(PID, 7, "user") != ns)
    check("shared-NS nutzerübergreifend gleich", R.kv_ns(PID, 1, "shared") == R.kv_ns(PID, 2, "shared"))
    check("user-NS pro Nutzer verschieden", R.kv_ns(PID, 1, "user") != R.kv_ns(PID, 2, "user"))

    R.kv_set(PID, ns, "tama", "state", {"xp": 0, "level": 1})
    check("kv_get liest gesetzten Wert", R.kv_get(PID, ns, "tama", "state")["xp"] == 0)
    v = R.kv_patch(PID, ns, "tama", "state", {"$inc": {"xp": 50}})
    check("$inc erhöht", v["xp"] == 50 and v["level"] == 1)
    v = R.kv_patch(PID, ns, "tama", "state", {"$inc": {"xp": 25}, "mood": "happy"})
    check("$inc + Merge gleichzeitig", v["xp"] == 75 and v["mood"] == "happy")
    check("kv_list findet Eintrag", len(R.kv_list(PID, ns, "tama")) == 1)
    check("kv_delete entfernt", R.kv_delete(PID, ns, "tama", "state") == 1)
    check("nach Delete leer", R.kv_get(PID, ns, "tama", "state") is None)


def test_event_bus():
    async def run():
        sid, q = plugin_bus.subscribe(["jarvis/plugin/ci_testplug/#"])
        n = await plugin_bus.publish("jarvis/plugin/ci_testplug/xp", {"d": 1})
        ev = await asyncio.wait_for(q.get(), 3)
        check("publish erreicht passenden Subscriber", n == 1 and ev["payload"]["d"] == 1)
        n2 = await plugin_bus.publish("jarvis/plugin/anders/xp", {})
        check("Fremd-Topic erreicht ihn nicht", n2 == 0)

        # Core-Event-Bridge
        automations.add_listener(plugin_bus.forward_core_event)
        plugin_bus.add_topics(sid, ["jarvis/core/#"])
        automations.emit("timer_elapsed", {"label": "x"})
        ev2 = await asyncio.wait_for(q.get(), 3)
        check("Core-Event als jarvis/core/* gespiegelt", ev2["topic"] == "jarvis/core/timer_elapsed")
        plugin_bus.unsubscribe(sid)

    asyncio.run(run())


def main():
    for t in (test_registry_and_keys, test_kv_store, test_event_bus):
        print(f"\n## {t.__name__}")
        t()
    R.delete(PID)
    print(f"\n{'='*48}")
    if _failures:
        print(f"FEHLGESCHLAGEN: {len(_failures)} → {_failures}")
        sys.exit(1)
    print("ALLE PLUGIN-TESTS BESTANDEN ✓")


if __name__ == "__main__":
    main()
