"""
Token-bewusstes Kontext-Budget (#3).

Statt nur einzelne Tool-Ergebnisse auf Zeichen zu kappen, wird hier der GESAMTE Prompt
(System + Verlauf + aktuelle Frage) gegen das Kontextfenster des Modells budgetiert. Passt der
Verlauf nicht, werden die ÄLTESTEN Turns getrimmt und ihr Inhalt in eine rollierende
Zusammenfassung überführt (extraktiv → keine zusätzliche LLM-Latenz; per Config auf LLM-
Zusammenfassung erweiterbar). So bleibt ein langes Gespräch lauffähig, ohne dass llama.cpp mit
`exceed_context_size_error` abbricht.

Token werden grob über Zeichen/Token geschätzt (deutsch ~3.0). Bewusst konservativ.
"""
from __future__ import annotations

import math

import debug

# Reserve, die NICHT für den Verlauf zur Verfügung steht: Generierung (max_tokens) + Tool-Schemas
# (~6–7k Tokens) + System-Overhead + Sicherheitsabstand.
_DEFAULT_RESERVE_TOKENS = 8000


def _chars_per_token(cfg: dict) -> float:
    try:
        return max(2.0, float(cfg.get("chars_per_token", 3.0)))
    except Exception:
        return 3.0


def estimate_tokens(text: str, cfg: dict) -> int:
    return math.ceil(len(text or "") / _chars_per_token(cfg))


def _msg_tokens(msg: dict, cfg: dict) -> int:
    return estimate_tokens(str(msg.get("content") or ""), cfg) + 4   # +4 Rollen-/Format-Overhead


def fit(sid: str, system_text: str, history: list[dict], user_msg: str, cfg: dict):
    """Trimmt `history` so, dass System + Verlauf + Frage ins Kontextfenster passen.
    Getrimmte (älteste) Turns wandern in die rollierende Zusammenfassung der Session.
    Gibt den behaltenen (jüngsten) Verlauf zurück."""
    n_ctx = int(cfg.get("llm_ctx", 32768))
    reserve = int(cfg.get("llm_max_tokens", 1024)) + int(cfg.get("ctx_reserve_tokens", _DEFAULT_RESERVE_TOKENS))
    budget = n_ctx - reserve - estimate_tokens(system_text, cfg) - estimate_tokens(user_msg, cfg)
    if budget < 512:
        budget = 512   # absolute Untergrenze, sonst gäbe es gar keinen Verlauf

    kept: list[dict] = []
    used = 0
    dropped: list[dict] = []
    # Von neu nach alt behalten, bis das Budget erschöpft ist.
    for msg in reversed(history):
        t = _msg_tokens(msg, cfg)
        if used + t <= budget:
            kept.append(msg)
            used += t
        else:
            dropped.append(msg)
    kept.reverse()
    dropped.reverse()   # chronologisch für die Zusammenfassung

    if dropped:
        _roll_summary(sid, dropped, cfg)
        debug.log("context_trim", session=sid, dropped=len(dropped), kept=len(kept),
                  budget_tokens=budget, used_tokens=used)
    return kept


def _roll_summary(sid: str, dropped: list[dict], cfg: dict) -> None:
    """Getrimmte Turns extraktiv in die Session-Zusammenfassung überführen (zeilenweise, gekappt)."""
    from session_hub import hub   # Singleton; session_hub importiert context_budget NICHT → kein Zyklus
    prev = hub.get_summary(sid)
    lines = []
    for m in dropped:
        role = m.get("role")
        content = " ".join(str(m.get("content") or "").split())
        if not content:
            continue
        tag = "Nutzer" if role == "user" else "Jarvis"
        lines.append(f"{tag}: {content[:200]}")
    if not lines:
        return
    merged = (prev + "\n" + "\n".join(lines)).strip() if prev else "\n".join(lines)
    # Zusammenfassung selbst begrenzen (jüngste Einträge behalten).
    cap = int(cfg.get("summary_max_chars", 1500))
    if len(merged) > cap:
        merged = merged[-cap:]
        merged = merged[merged.find("\n") + 1:] if "\n" in merged else merged
    hub.set_summary(sid, merged)
