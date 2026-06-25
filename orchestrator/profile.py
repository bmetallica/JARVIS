"""
Agent-kuratiertes Nutzermodell (Phase 3).

Ein kohärenter, fortgeschriebener Steckbrief pro Nutzer (Vorlieben, Rollen, Projekte, Tonfall) —
die Zusammenfassungs-Schicht ÜBER den atomaren pgvector-Fakten (`save_memory`). Wird periodisch aus
dem jüngsten Gesprächsverlauf per LLM **eingearbeitet** (merge, nicht anhängen) und in den
System-Prompt des erkannten Sprechers eingeblendet.
"""
from __future__ import annotations

import services
import store

_MAX_CHARS = 2500


def get(user_id) -> str:
    try:
        return store.profile_get(user_id)
    except Exception:
        return ""


def update_from_history(cfg: dict, user_id, history_tail: list[dict]) -> None:
    """Jüngsten Verlauf ins bestehende Profil einarbeiten (best-effort, synchron im Hintergrund-Thread)."""
    if user_id is None or not history_tail:
        return
    prev = get(user_id)
    convo = "\n".join(f"{'Nutzer' if m.get('role') == 'user' else 'Jarvis'}: {m.get('content', '')}"
                      for m in history_tail)[:4000]
    messages = [
        {"role": "system", "content":
            "Du pflegst ein KNAPPES, sachliches Nutzerprofil für einen Sprachassistenten. Arbeite neue, dauerhaft "
            "relevante Informationen (Name, Rolle, Vorlieben, Projekte, wiederkehrende Aufgaben, Tonfall-Wunsch) in "
            "das bestehende Profil EIN — fasse zusammen statt anzuhängen, entferne Veraltetes, erfinde nichts. "
            f"Gib NUR das aktualisierte Profil als kurze Stichpunktliste zurück (max. {_MAX_CHARS} Zeichen)."},
        {"role": "user", "content": f"Bestehendes Profil:\n{prev or '(noch leer)'}\n\nNeuer Gesprächsausschnitt:\n{convo}"},
    ]
    try:
        res = services.llm_call(messages, cfg, None, False)
    except Exception:
        return
    new = (res.get("content") or "").strip()[:_MAX_CHARS]
    if new and new != prev:
        try:
            store.profile_set(user_id, new)
        except Exception:
            pass
