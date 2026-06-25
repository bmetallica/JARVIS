#!/usr/bin/env python3
"""
Unit-Tests OHNE Modell/DB — laufen überall, schnell, deterministisch.

Lauf:  python3 tests/run_unit.py          (eigener Mini-Runner, exit!=0 bei Fehler)
       pytest tests/run_unit.py           (falls pytest installiert; test_*-Funktionen)

Deckt die in dieser Session gebaute Logik ab: Kontext-Budget (#3), Observability (#4),
MCP Deferred-Loading (#7). Persistenz (#2) braucht Postgres → separat (Smoke gegen den Dienst).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── #3 Kontext-Budget ────────────────────────────────────────────────────────
def test_budget_keeps_all_when_small():
    import context_budget
    cfg = {"llm_ctx": 32768, "ctx_reserve_tokens": 8000, "llm_max_tokens": 1024, "chars_per_token": 3.0}
    hist = [{"role": "user", "content": "kurz"}, {"role": "assistant", "content": "ok"}]
    kept = context_budget.fit("t_small", "SYS", hist, "frage", cfg)
    assert kept == hist, "kleiner Verlauf darf nicht getrimmt werden"


def test_budget_trims_and_summarizes():
    import context_budget
    from session_hub import hub
    cfg = {"llm_ctx": 4000, "ctx_reserve_tokens": 500, "llm_max_tokens": 500, "chars_per_token": 3.0,
           "summary_max_chars": 1500}
    hist = [{"role": "user", "content": f"Thema {i}: " + "x" * 3000} for i in range(8)]
    kept = context_budget.fit("t_trim", "SYS", hist, "frage", cfg)
    assert 0 < len(kept) < len(hist), f"erwartete Trimmung, behielt {len(kept)}/{len(hist)}"
    assert kept == hist[-len(kept):], "es müssen die JÜNGSTEN Nachrichten behalten werden"
    assert hub.get_summary("t_trim"), "getrimmte Turns müssen in die Zusammenfassung wandern"


def test_estimate_tokens_monotonic():
    import context_budget
    cfg = {"chars_per_token": 3.0}
    assert context_budget.estimate_tokens("abc", cfg) <= context_budget.estimate_tokens("abcdef", cfg)


# ── #4 Observability ─────────────────────────────────────────────────────────
def test_metrics_count_turns_and_tools():
    import debug
    before = debug.metrics()
    debug.log("turn_done", ms=1200, reply="x")
    debug.log("tool", name="fetch_url", result="y")
    debug.log("tool", name="fetch_url", result="y")
    after = debug.metrics()
    assert after["turns"] == before["turns"] + 1
    assert after["tool_calls"] == before["tool_calls"] + 2
    assert after["tool_calls_by_name"].get("fetch_url", 0) >= 2


def test_metrics_error_rate_and_502():
    import debug
    debug.log("turn_done", ms=10, error=True)
    debug.log("llm_error", error="502 Bad Gateway")
    m = debug.metrics()
    assert m["turns_error"] >= 1 and m["llm_502"] >= 1


# ── #7 MCP Deferred-Loading ──────────────────────────────────────────────────
def test_mcp_deferred_catalog_and_schemas():
    import mcp_hub
    mcp_hub._cache.clear()
    mcp_hub._cache["domoticz"] = {"enabled": True, "tools": [
        {"name": "set_switch", "description": "Schalter schalten", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "get_temp", "description": "Temperatur lesen", "inputSchema": {"type": "object", "properties": {}}},
    ]}
    assert mcp_hub.has_servers() is True
    cat = mcp_hub.catalog_hint()
    assert "domoticz" in cat and "set_switch" in cat
    # search
    found = mcp_hub.search("temp")
    assert any(f["full_name"] == "mcp__domoticz__get_temp" for f in found)
    # schemas_for nur für geladene Tools, korrekt benannt
    sch = mcp_hub.schemas_for(["mcp__domoticz__set_switch"])
    assert len(sch) == 1 and sch[0]["function"]["name"] == "mcp__domoticz__set_switch"
    # unbekanntes / nicht geladenes Tool → kein Schema
    assert mcp_hub.schemas_for(["mcp__domoticz__nope", "mcp__other__x"]) == []
    mcp_hub._cache.clear()


def test_mcp_disabled_server_hidden():
    import mcp_hub
    mcp_hub._cache.clear()
    mcp_hub._cache["off"] = {"enabled": False, "tools": [{"name": "x", "description": "", "inputSchema": {}}]}
    assert mcp_hub.has_servers() is False
    assert mcp_hub.catalog_hint() == ""
    mcp_hub._cache.clear()


# ── #1 Log-Redaction ─────────────────────────────────────────────────────────
def test_log_redaction():
    import debug
    rec = {"kind": "turn", "message": "geheimer Klartext", "ms": 12}
    assert debug._redact(rec, {"log_redact": False}) == rec, "ohne Flag unverändert"
    red = debug._redact(rec, {"log_redact": True})
    assert "geheimer" not in red["message"] and "redacted" in red["message"]
    assert red["ms"] == 12, "Nicht-PII-Felder bleiben erhalten"


# ── #10 Verify-by-Tool ───────────────────────────────────────────────────────
def test_tool_failure_detection():
    import tools
    assert tools._looks_failed("Fehler: nix da") is True
    assert tools._looks_failed("Berechtigung verweigert: …") is True
    assert tools._looks_failed("Abruf fehlgeschlagen: timeout") is True
    assert tools._looks_failed("Timer gesetzt.") is False


def test_unverified_claim_flagged():
    import app, debug
    base = debug.metrics()["unverified_claims"]
    # „gesendet" behauptet, aber KEIN Tool lief → muss geflaggt werden
    app._finalize_turn({"turn_tool_calls": []}, "t_claim", "Die Nachricht wurde gesendet.")
    assert debug.metrics()["unverified_claims"] == base + 1
    # mit geglücktem send_message → NICHT flaggen
    app._finalize_turn({"turn_tool_calls": [{"name": "send_message", "ok": True}]},
                       "t_claim", "Die Nachricht wurde gesendet.")
    assert debug.metrics()["unverified_claims"] == base + 1
    # reine Auskunft ohne Vollzugs-Behauptung → NICHT flaggen
    app._finalize_turn({"turn_tool_calls": []}, "t_claim", "Das Wetter morgen ist sonnig.")
    assert debug.metrics()["unverified_claims"] == base + 1


# ── #12 Barge-in: Cancel-Registry ────────────────────────────────────────────
def test_cancel_registry():
    from session_hub import hub
    hub.clear_cancel("t_cancel")
    assert hub.is_cancelled("t_cancel") is False
    hub.request_cancel("t_cancel")
    assert hub.is_cancelled("t_cancel") is True
    hub.clear_cancel("t_cancel")
    assert hub.is_cancelled("t_cancel") is False


# ── Phase 0: Modell-Registry ─────────────────────────────────────────────────
def test_model_registry_helpers():
    import config
    cfg = {"models": [{"id": "gemma4-12b", "role": "agent", "ctx_total": 49152, "slots": 2},
                      {"id": "qwen3-14b", "role": "subagent", "ctx_total": 40960, "slots": 1}],
           "llm_model": "fallback", "llm_ctx": 8192, "vision_model": "v"}
    assert config.model_for("agent", cfg) == "gemma4-12b"
    assert config.ctx_for("agent", cfg) == 24576              # 49152 / 2 Slots
    assert config.model_for("subagent", cfg) == "qwen3-14b"
    assert config.model_for("vision", cfg) == "v"             # Fallback auf vision_model
    assert config.ctx_for("fast", cfg) == 8192                # unbekannte Rolle → llm_ctx


# ── Phase 4: Subagent-Rekursionssperre ───────────────────────────────────────
def test_subagent_recursion_guard():
    import subagent
    assert subagent.can_spawn({}) is True
    assert subagent.can_spawn({"subagent_depth": 0}) is True
    assert subagent.can_spawn({"subagent_depth": 1}) is False


# ── Kalender: ICS-Parser + Zeitzonen ─────────────────────────────────────────
def test_ics_parser():
    import calendars
    sample = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:x1\r\nSUMMARY:Mittag mit\r\n  Kollegen\r\n"
              "DTSTART;TZID=Europe/Berlin:20260702T123000\r\nDTEND;TZID=Europe/Berlin:20260702T133000\r\n"
              "LOCATION:Kantine\r\nRRULE:FREQ=WEEKLY\r\nEND:VEVENT\r\n"
              "BEGIN:VEVENT\r\nUID:x2\r\nSUMMARY:Urlaub\r\nDTSTART;VALUE=DATE:20260710\r\nEND:VEVENT\r\nEND:VCALENDAR")
    evs = calendars.parse_ics(sample)
    assert len(evs) == 2
    e = evs[0]
    assert e["title"] == "Mittag mit Kollegen"          # Zeilenfaltung aufgelöst
    assert e["start_ts"].hour == 10                      # 12:30 Berlin (CEST) → 10:30 UTC
    assert e["rrule"] == "FREQ=WEEKLY" and e["location"] == "Kantine"
    assert evs[1]["all_day"] is True


def test_calendar_parse_dt():
    import calendars
    from datetime import timezone
    dt = calendars.parse_dt("2026-06-26T15:00")          # naiv → Europe/Berlin → UTC
    assert dt.tzinfo == timezone.utc and dt.hour == 13    # 15:00 CEST → 13:00 UTC
    z = calendars.parse_dt("2026-06-26T13:00:00Z")
    assert z.hour == 13


# ── Mini-Runner (ohne pytest) ────────────────────────────────────────────────
def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} Tests bestanden.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
