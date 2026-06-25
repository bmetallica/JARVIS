"""
To-do-Listen pro Nutzer.

  • Punkte mit optionalem Fälligkeitsdatum; datierte Punkte erzeugen einen Ganztags-Kalendereintrag
    (☑-Präfix), der bei „erledigt"/Löschen wieder verschwindet.
  • Ähnlichkeitssuche zum Abhaken/Löschen (lexikalisch via difflib + semantischer Embedding-Fallback),
    da gesprochene Begriffe selten exakt dem Eintrag entsprechen.
  • Hardlink-Zugriff (Token) für eine smartphone-optimierte Abhak-Oberfläche ohne Login.
"""
from __future__ import annotations

import difflib
import re
import secrets
from datetime import date

import store

_init = False


def init() -> None:
    global _init
    if _init:
        return
    store.init()
    with store._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS todos (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            text TEXT NOT NULL,
            done BOOLEAN NOT NULL DEFAULT FALSE,
            due_date DATE,
            calendar_event_id BIGINT,
            created_at TIMESTAMPTZ DEFAULT now(),
            done_at TIMESTAMPTZ);""")
        c.execute("CREATE INDEX IF NOT EXISTS todos_user_idx ON todos (user_id, done);")
        c.execute("""CREATE TABLE IF NOT EXISTS todo_share (
            user_id BIGINT PRIMARY KEY,
            token TEXT UNIQUE NOT NULL);""")
    _init = True


# ── Hardlink-Token ────────────────────────────────────────────────────────────
def share_token(user_id: int) -> str:
    init()
    with store._conn() as c:
        row = c.execute("SELECT token FROM todo_share WHERE user_id=%s;", (user_id,)).fetchone()
        if row:
            return row[0]
        tok = secrets.token_urlsafe(20)
        c.execute("INSERT INTO todo_share (user_id, token) VALUES (%s,%s);", (user_id, tok))
    return tok


def user_by_token(token: str):
    init()
    with store._conn() as c:
        row = c.execute("SELECT user_id FROM todo_share WHERE token=%s;", (token,)).fetchone()
    return row[0] if row else None


# ── CRUD ──────────────────────────────────────────────────────────────────────
def _row(r) -> dict:
    return {"id": r[0], "user_id": r[1], "text": r[2], "done": r[3],
            "due_date": str(r[4]) if r[4] else None, "calendar_event_id": r[5]}


def get(todo_id: int) -> dict | None:
    init()
    with store._conn() as c:
        r = c.execute("SELECT id,user_id,text,done,due_date,calendar_event_id FROM todos WHERE id=%s;",
                      (todo_id,)).fetchone()
    return _row(r) if r else None


def list_todos(user_id: int, include_done: bool = False) -> list[dict]:
    init()
    q = ("SELECT id,user_id,text,done,due_date,calendar_event_id FROM todos WHERE user_id=%s "
         + ("" if include_done else "AND done=FALSE ")
         + "ORDER BY done, due_date NULLS LAST, created_at;")
    with store._conn() as c:
        rows = c.execute(q, (user_id,)).fetchall()
    return [_row(r) for r in rows]


def _link_calendar(user_id: int, text: str, due: str | None) -> int | None:
    """Datierter Punkt → Ganztags-Kalendereintrag im persönlichen Kalender."""
    if not due:
        return None
    try:
        import calendars
        cal = calendars.ensure_user_calendar(user_id)
        ev = calendars.add_event(cal["id"], user_id, "☑ " + text, due, None, all_day=True)
        return ev["id"]
    except Exception:
        return None


def _unlink_calendar(event_id: int | None) -> None:
    if not event_id:
        return
    try:
        import calendars
        calendars.delete_event(event_id)
    except Exception:
        pass


def add(user_id: int, text: str, due: str | None = None) -> dict:
    init()
    text = (text or "").strip()
    ev_id = _link_calendar(user_id, text, due)
    with store._conn() as c:
        r = c.execute("INSERT INTO todos (user_id, text, due_date, calendar_event_id) VALUES (%s,%s,%s,%s) "
                      "RETURNING id,user_id,text,done,due_date,calendar_event_id;",
                      (user_id, text, due, ev_id)).fetchone()
    return _row(r)


def set_done(todo_id: int, done: bool = True) -> dict | None:
    init()
    t = get(todo_id)
    if not t:
        return None
    with store._conn() as c:
        c.execute("UPDATE todos SET done=%s, done_at=CASE WHEN %s THEN now() ELSE NULL END WHERE id=%s;",
                  (done, done, todo_id))
    # Erledigt → Kalendereintrag entfernen; wieder offen → neu anlegen.
    if done:
        _unlink_calendar(t["calendar_event_id"])
        if t["calendar_event_id"]:
            with store._conn() as c:
                c.execute("UPDATE todos SET calendar_event_id=NULL WHERE id=%s;", (todo_id,))
    elif t["due_date"] and not t["calendar_event_id"]:
        ev = _link_calendar(t["user_id"], t["text"], t["due_date"])
        with store._conn() as c:
            c.execute("UPDATE todos SET calendar_event_id=%s WHERE id=%s;", (ev, todo_id))
    return get(todo_id)


def remove(todo_id: int) -> bool:
    init()
    t = get(todo_id)
    if not t:
        return False
    _unlink_calendar(t["calendar_event_id"])
    with store._conn() as c:
        c.execute("DELETE FROM todos WHERE id=%s;", (todo_id,))
    return True


# ── Ähnlichkeitssuche ─────────────────────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\wäöüß ]+", " ", (s or "").lower())).strip()


def _lex_score(q: str, t: str) -> float:
    a, b = _norm(q), _norm(t)
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    sub = 1.0 if (a in b or b in a) else 0.0
    return max(0.6 * ratio + 0.4 * jac, 0.85 * sub)


def match(user_id: int, query: str, due: str | None = None, threshold: float = 0.45) -> dict | None:
    """Besten offenen To-do-Punkt zur (ungefähren) Beschreibung finden. Optional auf ein Datum eingrenzen."""
    items = [t for t in list_todos(user_id, include_done=False)]
    if due:
        dated = [t for t in items if t["due_date"] == due]
        if len(dated) == 1 and not (query or "").strip():
            return dated[0]
        if dated:
            items = dated
    if not items:
        return None
    scored = sorted(((t, _lex_score(query, t["text"])) for t in items), key=lambda x: -x[1])
    best, score = scored[0]
    if score >= threshold:
        return best
    # Semantischer Fallback (Synonyme): Query + Texte einmal einbetten und cosinus-vergleichen.
    try:
        import services
        import config
        vecs = services.embed([query] + [t["text"] for t in items], config.get(), task="search_document")
        qv = vecs[0]

        def cos(a, b):
            import math
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
            return dot / (na * nb) if na and nb else 0.0
        sem = sorted(((items[i], cos(qv, vecs[i + 1])) for i in range(len(items))), key=lambda x: -x[1])
        if sem[0][1] >= 0.6:
            return sem[0][0]
    except Exception:
        pass
    return None
