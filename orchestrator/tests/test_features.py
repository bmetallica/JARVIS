#!/usr/bin/env python3
"""
Tests für die 2026-07-02 umgesetzten Verbesserungen (erweiterungen.md).

Braucht Postgres (store.DSN) für einige Fälle — KEIN LLM/GPU nötig.
Lauf:  python3 tests/test_features.py       (Mini-Runner, exit!=0 bei Fehler)
"""
import asyncio
import os
import pathlib
import sys
import tempfile
import shutil
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_failures = []


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        _failures.append(name)


def test_switch_vs_read_classification():
    """Read/Switch-Split: Schaltbefehle vs. Lese-/Messfragen (Smart-Home-Beschleunigung)."""
    import app
    for msg, exp in [("Schalte das Wohnzimmerlicht an.", True), ("mach das licht aus", True),
                     ("dimme das licht auf 30%", True), ("stelle die heizung auf 22 grad", True),
                     ("wie ist mein stromverbrauch laut stromzähler?", False),
                     ("welche lampen gibt es?", False), ("ist das licht an?", False)]:
        check(f"_is_switch_command({msg[:34]!r}) == {exp}", app._is_switch_command(msg) == exp)


def test_unverified_failure_retry():
    """1.2: halluzinierter Fehlschlag ohne Tool-Beleg → Denk-Retry; echter Tool-Fehler → kein Retry."""
    import app
    check("Fehlschlag ohne Tool → Retry",
          app._needs_think_retry("Der Stromzähler ist derzeit nicht erreichbar.", {"turn_tool_calls": []})
          == "unverifizierte Fehlschlags-Behauptung")
    check("echter Tool-Fehler → kein Retry",
          app._needs_think_retry("Das hat nicht geklappt.",
                                 {"turn_tool_calls": [{"name": "x", "ok": False}]}) is None)
    check("Erfolgs-Halluzination weiter erkannt",
          app._needs_think_retry("Ich habe das Licht eingeschaltet.", {"turn_tool_calls": []})
          == "unverifizierte Erfolgsbehauptung")
    check("normale Antwort → kein Retry",
          app._needs_think_retry("Der Verbrauch beträgt 2555 Watt.", {"turn_tool_calls": []}) is None)


def test_fast_confirm():
    """4.2: deterministische Schalt-Bestätigung statt LLM-Runde."""
    import app
    cfg = {"smarthome_fast_confirm": True}
    ctx = {"accel_devices": {"14": "Wohnzimmerlicht"}}
    ok = '{\n\t"status" : "OK",\n\t"title" : "SwitchLight"\n}'
    check("On-Satz", app._fast_confirm("mcp__s__set_switch_state", {"idx": 14, "state": "On"}, ok, ctx, cfg)
          == "Wohnzimmerlicht ist jetzt an.")
    check("Dimmer-Satz", app._fast_confirm("mcp__s__set_dimmer_level", {"idx": 14, "level": 30}, ok, ctx, cfg)
          == "Wohnzimmerlicht ist jetzt auf 30 % gedimmt.")
    check("Fehler → None", app._fast_confirm("mcp__s__set_switch_state", {"idx": 14, "state": "On"},
                                             "[FEHLER] 400", ctx, cfg) is None)
    check("kein Accel-Turn → None", app._fast_confirm("mcp__s__set_switch_state", {"idx": 14, "state": "On"},
                                                      ok, {}, cfg) is None)
    check("Schalter aus → None", app._fast_confirm("mcp__s__set_switch_state", {"idx": 14, "state": "On"},
                                                   ok, ctx, {"smarthome_fast_confirm": False}) is None)


def test_plugin_cron_trigger():
    """5.5: Scheduler-Cron daily + weekly."""
    import app
    check("daily", app._plugin_trigger({"cron": "0 6 * * *"}) == {"type": "daily", "time": "06:00"})
    check("weekly Mo-Fr", app._plugin_trigger({"cron": "30 7 * * 1-5"})
          == {"type": "weekly", "time": "07:30", "weekdays": [0, 1, 2, 3, 4]})
    check("weekly So+Sa", app._plugin_trigger({"cron": "0 9 * * 0,6"})
          == {"type": "weekly", "time": "09:00", "weekdays": [5, 6]})


def test_obsidian_categories_daily_search():
    """7.1–7.3: Mehrfach-Kategorien, Tagebuch-Modus, Notiz-Suche (Temp-Vault, ohne LLM)."""
    import obsidian, config
    tmp = tempfile.mkdtemp()
    try:
        cfg = {**config.get(), "obsidian_vaults": {"t": tmp}, "obsidian_autolink": False,
               "obsidian_note_mode": "file"}
        d = time.strftime("%Y-%m-%d")
        ok, _ = obsidian.save_note("t", "Schulgespräch gut", title="Schulgespräch",
                                   category="ADHS, Schule", cfg=cfg)
        p = os.path.join(tmp, "ADHS", f"{d} Schulgespräch.md")
        c = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        check("Mehrfach-Kategorien (Ordner=1., Tags=alle)", ok and "- ADHS" in c and "- Schule" in c)
        obsidian.save_note("t", "Eintrag 1", category="Tagebuch", cfg=cfg)
        obsidian.save_note("t", "Eintrag 2", category="Tagebuch", cfg=cfg)
        tf = os.path.join(tmp, "Tagebuch", f"{d}.md")
        tc = open(tf, encoding="utf-8").read() if os.path.exists(tf) else ""
        check("Tagebuch: eine Tagesdatei, 2 Einträge", tc.count("- **") == 2 and tc.count("---") == 2)
        ok, msg = obsidian.search_notes("t", "Schulgespräch", cfg=cfg)
        check("search_notes findet Notiz", ok and "Schulgespräch" in msg)
        ok, msg = obsidian.search_notes("t", "nixdagibtsnicht", cfg=cfg)
        check("search_notes leer sauber", "Keine Notizen" in msg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bus_unsubscribe_and_rate():
    """5.4 + 3.3: WS-unsubscribe wirkt; Rate-Limiter zählt korrekt."""
    import plugin_bus
    import plugins_registry as R

    async def run():
        sid, q = plugin_bus.subscribe(["a/#", "b/#"])
        plugin_bus.remove_topics(sid, ["a/#"])
        check("unsubscribe entfernt Topic", await plugin_bus.publish("a/x", {}) == 0)
        check("übrige Topics bleiben", await plugin_bus.publish("b/x", {}) == 1)
        plugin_bus.unsubscribe(sid)
    asyncio.run(run())
    cfg = {"plugin_rate_limit_per_min": 2, "plugin_rate_limit_heavy_per_min": 1}
    kid = f"kt-{time.time()}"
    check("rate: 1+2 ok, 3. geblockt",
          R.rate_check(kid, False, cfg)[0] and R.rate_check(kid, False, cfg)[0]
          and not R.rate_check(kid, False, cfg)[0])
    check("rate: heavy separater Bucket", R.rate_check(kid, True, cfg)[0])
    check("rate: 0 = aus", R.rate_check(kid, False, {"plugin_rate_limit_per_min": 0})[0])


def test_session_prune():
    """2.2: RAM-Zustand toter Sessions wird geräumt, verbundene bleiben."""
    from session_hub import hub
    sid = f"prune-test-{time.time()}"
    hub._history[sid] = [{"role": "user", "content": "x"}]
    hub.prune(86400)                                   # 1. Lauf: Stempel
    hub._prune_seen[sid] = time.time() - 90000         # künstlich altern
    n = hub.prune(86400)
    check("tote Session geräumt", sid not in hub._history and n >= 1)


def test_config_secret_masking():
    """3.2: Token maskiert raus, maskierter Wert wird beim Speichern verworfen."""
    import app
    masked = app._mask_secrets({"telegram_bot_token": "123456:ABCDEF", "x": 1})
    check("Token maskiert", masked["telegram_bot_token"] == "…CDEF" and masked["x"] == 1)
    check("leer bleibt leer", app._mask_secrets({"telegram_bot_token": ""})["telegram_bot_token"] == "")


def test_external_marker_and_error_prefix():
    """3.1/1.6: Konstanten + Fehlerpräfix-Konvention vorhanden."""
    import tools
    check("Marker-Konstanten", tools._EXT_MARK_START.startswith("[EXTERNE INHALTE")
          and tools._EXT_MARK_END == "[/EXTERNE INHALTE]")
    check("fetch_url ist externes Tool", "fetch_url" in tools._EXTERNAL_CONTENT_TOOLS)
    check("_looks_failed erkennt [FEHLER]", tools._looks_failed("[FEHLER] kaputt"))


def test_telegram_poller_survives_handler_error():
    """Eine kaputte Nachricht darf den Telegram-Poller NICHT beenden (Regression 2026-10-13:
    ein TypeError aus einer Weiterleitung hat den Task getötet — danach kam gar nichts mehr an,
    während der Dienst weiter „active" war)."""
    import app
    import debug
    debug.enabled = True
    sent, handled, calls = [], [], {"n": 0}
    orig = (app.messaging.enabled, app.messaging.is_verified, app.messaging.send_to_chat,
            app.messaging.get_updates, app._handle_telegram_update)
    app.messaging.enabled = lambda: True
    app.messaging.is_verified = lambda chat: True
    app.messaging.send_to_chat = lambda chat, text: sent.append(text) or True

    async def flaky(u):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TypeError("absichtlich kaputt")
        handled.append(u.get("update_id"))

    app._handle_telegram_update = flaky
    served = {"done": False}

    def get_updates(offset, limit):
        if served["done"]:
            raise asyncio.CancelledError
        served["done"] = True
        return [{"update_id": 1, "message": {"chat": {"id": 1}, "text": "a"}},
                {"update_id": 2, "message": {"chat": {"id": 1}, "text": "b"}}]

    app.messaging.get_updates = get_updates
    try:
        async def run():
            try:
                await asyncio.wait_for(app._telegram_poller(), timeout=20)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        asyncio.run(run())
        check("beide Nachrichten zugestellt", calls["n"] == 2)
        check("nach dem Fehler wird weiterverarbeitet", handled == [2])
        check("Absender wird informiert", any("schiefgegangen" in s for s in sent))
        check("Fehler landet im Protokoll",
              any(e["kind"] == "telegram_handler_error" for e in debug.events()))
    finally:
        (app.messaging.enabled, app.messaging.is_verified, app.messaging.send_to_chat,
         app.messaging.get_updates, app._handle_telegram_update) = orig


def test_debug_log_kind_collision():
    """debug.log(kind=…) darf nicht werfen: der Ereignistyp ist positions-only, ein
    gleichnamiges Datenfeld landet harmlos in data (Regression 2026-10-13)."""
    import debug
    debug.enabled = True
    debug.log("probe_kind", kind="text", prefix="rezept")
    ev = debug.events()[-1]
    check("kein TypeError bei kind= als Datenfeld", ev["kind"] == "probe_kind")
    check("übriges Datenfeld bleibt erhalten", ev.get("prefix") == "rezept")


def test_automation_loop_guarded():
    """Der Scheduler-Schleifenkörper ist abgesichert — sonst legt eine Ausnahme alle
    geplanten Selbstläufe lautlos still."""
    import inspect
    import automations
    src = inspect.getsource(automations.AutomationManager._loop)
    check("Scheduler-Loop fängt Ausnahmen", "except Exception" in src)
    check("Scheduler-Loop protokolliert sie", "automation_loop_error" in src)


def _isolate_logs():
    """Testläufe dürfen NICHT ins Betriebs-Protokoll schreiben — sonst stehen erfundene
    Ereignisse (etwa eine Weiterleitung mit HTTP 500) in logs/events-*.jsonl und führen bei
    der nächsten echten Fehlersuche in die Irre."""
    import tempfile
    import debug
    debug._LOG_DIR = pathlib.Path(tempfile.mkdtemp(prefix="jarvis-test-logs-"))


def main():
    _isolate_logs()
    tests = [test_switch_vs_read_classification, test_unverified_failure_retry, test_fast_confirm,
             test_plugin_cron_trigger, test_obsidian_categories_daily_search,
             test_bus_unsubscribe_and_rate, test_session_prune, test_config_secret_masking,
             test_external_marker_and_error_prefix,
             test_telegram_poller_survives_handler_error, test_debug_log_kind_collision,
             test_automation_loop_guarded]
    for t in tests:
        print(f"\n## {t.__name__}")
        try:
            t()
        except Exception as e:
            print(" FAIL (Exception)", e)
            _failures.append(t.__name__)
    print(f"\n{'=' * 48}")
    if _failures:
        print(f"FEHLGESCHLAGEN: {len(_failures)} → {_failures}")
        sys.exit(1)
    print("ALLE FEATURE-TESTS BESTANDEN ✓")


if __name__ == "__main__":
    main()
