"""
Kalender — pro JARVIS-Nutzer ein persönlicher Kalender + ein gemeinsamer.

Funktionen:
  • Termine anlegen/ändern/löschen (mit Ort, Beschreibung, ganztägig, Wiederholung).
  • Freigaben zwischen Nutzern (read/write) — der gemeinsame Kalender ist für alle schreibbar.
  • iCal-Abo (nur LAN): je Kalender ein Token-Feed + ein kombinierter Feed pro Nutzer.

Zeitzonen: Eingaben ohne Offset gelten als Europe/Berlin; gespeichert wird als UTC (timestamptz).
iCal-Ausgabe in UTC (…Z) bzw. DATE für ganztägige Termine.

Tabellen liegen in derselben Postgres-DB wie der Vektor-Store (store._conn).
"""
from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone

import requests

import store

try:
    from zoneinfo import ZoneInfo
    LOCAL = ZoneInfo("Europe/Berlin")
except Exception:                       # Fallback, falls tzdata fehlt
    LOCAL = timezone(timedelta(hours=1))

_init = False
_RRULE = {"daily": "FREQ=DAILY", "weekly": "FREQ=WEEKLY", "monthly": "FREQ=MONTHLY", "yearly": "FREQ=YEARLY"}


def init() -> None:
    global _init
    if _init:
        return
    store.init()
    with store._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS calendars (
            id BIGSERIAL PRIMARY KEY,
            owner_user_id BIGINT,                       -- NULL = gemeinsamer Kalender
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'user',          -- 'user' | 'shared'
            ics_token TEXT UNIQUE NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now());""")
        c.execute("""CREATE TABLE IF NOT EXISTS calendar_events (
            id BIGSERIAL PRIMARY KEY,
            calendar_id BIGINT NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
            uid TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            description TEXT DEFAULT '',
            location TEXT DEFAULT '',
            start_ts TIMESTAMPTZ NOT NULL,
            end_ts TIMESTAMPTZ,
            all_day BOOLEAN DEFAULT FALSE,
            rrule TEXT DEFAULT '',
            created_by BIGINT,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now());""")
        c.execute("CREATE INDEX IF NOT EXISTS cal_events_cal_idx ON calendar_events (calendar_id, start_ts);")
        c.execute("""CREATE TABLE IF NOT EXISTS calendar_shares (
            calendar_id BIGINT NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            access TEXT NOT NULL DEFAULT 'read',         -- 'read' | 'write'
            PRIMARY KEY (calendar_id, user_id));""")
        c.execute("""CREATE TABLE IF NOT EXISTS calendar_user_token (
            user_id BIGINT PRIMARY KEY,
            token TEXT UNIQUE NOT NULL);""")
        c.execute("ALTER TABLE calendars ADD COLUMN IF NOT EXISTS source_url TEXT;")   # externe Abos
        c.execute("ALTER TABLE calendars ADD COLUMN IF NOT EXISTS last_sync TIMESTAMPTZ;")
        c.execute("ALTER TABLE calendars ADD COLUMN IF NOT EXISTS last_error TEXT;")
    _init = True


def _token() -> str:
    return secrets.token_urlsafe(24)


# ── Kalender anlegen/finden ───────────────────────────────────────────────────
def ensure_common() -> dict:
    init()
    with store._conn() as c:
        row = c.execute("SELECT id, owner_user_id, name, kind, ics_token FROM calendars WHERE kind='shared' LIMIT 1;").fetchone()
        if not row:
            row = c.execute("INSERT INTO calendars (owner_user_id, name, kind, ics_token) "
                            "VALUES (NULL, %s, 'shared', %s) RETURNING id, owner_user_id, name, kind, ics_token;",
                            ("Gemeinsam", _token())).fetchone()
    return _cal(row)


def ensure_user_calendar(user_id: int, username: str | None = None) -> dict:
    init()
    with store._conn() as c:
        row = c.execute("SELECT id, owner_user_id, name, kind, ics_token FROM calendars "
                        "WHERE owner_user_id=%s AND kind='user' LIMIT 1;", (user_id,)).fetchone()
        if not row:
            name = f"Kalender {username}" if username else f"Kalender #{user_id}"
            row = c.execute("INSERT INTO calendars (owner_user_id, name, kind, ics_token) "
                            "VALUES (%s, %s, 'user', %s) RETURNING id, owner_user_id, name, kind, ics_token;",
                            (user_id, name, _token())).fetchone()
    return _cal(row)


def _cal(row) -> dict:
    return {"id": row[0], "owner_user_id": row[1], "name": row[2], "kind": row[3], "ics_token": row[4]}


def get_calendar(calendar_id: int) -> dict | None:
    init()
    with store._conn() as c:
        row = c.execute("SELECT id, owner_user_id, name, kind, ics_token FROM calendars WHERE id=%s;",
                        (calendar_id,)).fetchone()
    return _cal(row) if row else None


# ── Zugriff ───────────────────────────────────────────────────────────────────
def access_level(user_id: int, cal: dict) -> str | None:
    """None | 'read' | 'write' | 'owner'."""
    if cal is None:
        return None
    if cal["kind"] == "shared":
        return "write"                              # gemeinsamer Kalender: alle dürfen schreiben
    if cal["kind"] == "external":                   # abonnierter Fremdkalender: nur lesen
        return "read" if cal["owner_user_id"] == user_id else None
    if cal["owner_user_id"] == user_id:
        return "owner"
    with store._conn() as c:
        row = c.execute("SELECT access FROM calendar_shares WHERE calendar_id=%s AND user_id=%s;",
                        (cal["id"], user_id)).fetchone()
    return row[0] if row else None


def list_accessible(user_id: int) -> list[dict]:
    """Alle Kalender, die der Nutzer sehen darf (eigener + gemeinsame + freigegebene) mit Zugriffsstufe."""
    init()
    ensure_user_calendar(user_id)
    ensure_common()
    out = []
    with store._conn() as c:
        rows = c.execute(
            "SELECT id, owner_user_id, name, kind, ics_token FROM calendars "
            "WHERE kind='shared' OR owner_user_id=%s "
            "OR id IN (SELECT calendar_id FROM calendar_shares WHERE user_id=%s) ORDER BY kind DESC, name;",
            (user_id, user_id)).fetchall()
    for r in rows:
        cal = _cal(r)
        cal["access"] = access_level(user_id, cal)
        out.append(cal)
    return out


def resolve_calendar(user_id: int, selector: str | None, username: str | None = None) -> dict | None:
    """selector: None/'own'/'mein' → eigener; 'common'/'gemeinsam'/'shared' → gemeinsamer;
    sonst Nutzername → dessen Kalender (sofern zugänglich)."""
    sel = (selector or "own").strip().lower()
    if sel in ("own", "mein", "meiner", "eigener", "persönlich", "personal", ""):
        return ensure_user_calendar(user_id, username)
    if sel in ("common", "gemeinsam", "shared", "geteilt", "allgemein"):
        return ensure_common()
    # Nutzername → dessen Kalender
    import auth
    u = auth.user_by_name(sel)
    if not u:
        return None
    cal = ensure_user_calendar(u["id"], u.get("username"))
    return cal if access_level(user_id, cal) else None


# ── Events ────────────────────────────────────────────────────────────────────
def parse_dt(s: str, all_day: bool = False):
    """ISO-String → aware UTC datetime. Naiv = Europe/Berlin. Ganztägig = lokale Mitternacht
    (als aware datetime gespeichert, damit die iCal-DATE-Ausgabe nicht um einen Tag verrutscht)."""
    s = (s or "").strip()
    if all_day:
        d = date.fromisoformat(s[:10])
        return datetime(d.year, d.month, d.day, tzinfo=LOCAL)
    s2 = s.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s2)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL)
    return dt.astimezone(timezone.utc)


def add_event(calendar_id: int, created_by: int, title: str, start: str, end: str | None = None,
              description: str = "", location: str = "", all_day: bool = False,
              recurrence: str = "") -> dict:
    init()
    sdt = parse_dt(start, all_day)
    edt = parse_dt(end, all_day) if end else None
    if not all_day and edt is None:
        edt = sdt + timedelta(hours=1)
    rrule = _RRULE.get((recurrence or "").strip().lower(), "")
    uid = secrets.token_hex(12) + "@jarvis"
    with store._conn() as c:
        row = c.execute(
            "INSERT INTO calendar_events (calendar_id, uid, title, description, location, start_ts, end_ts, "
            "all_day, rrule, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id;",
            (calendar_id, uid, title, description or "", location or "", sdt, edt, all_day, rrule, created_by),
        ).fetchone()
    return {"id": row[0], "uid": uid}


def get_event(event_id: int) -> dict | None:
    init()
    with store._conn() as c:
        r = c.execute("SELECT id, calendar_id, uid, title, description, location, start_ts, end_ts, all_day, "
                      "rrule, created_by FROM calendar_events WHERE id=%s;", (event_id,)).fetchone()
    if not r:
        return None
    return {"id": r[0], "calendar_id": r[1], "uid": r[2], "title": r[3], "description": r[4], "location": r[5],
            "start_ts": r[6], "end_ts": r[7], "all_day": r[8], "rrule": r[9], "created_by": r[10]}


def update_event(event_id: int, **fields) -> bool:
    init()
    ev = get_event(event_id)
    if not ev:
        return False
    all_day = bool(fields["all_day"]) if fields.get("all_day") is not None else ev["all_day"]
    cols, vals = [], []
    for k in ("title", "description", "location"):
        if fields.get(k) is not None:
            cols.append(f"{k}=%s"); vals.append(fields[k])
    new_start = parse_dt(fields["start"], all_day) if fields.get("start") is not None else None
    new_end = parse_dt(fields["end"], all_day) if fields.get("end") is not None else None
    if new_start is not None:
        cols.append("start_ts=%s"); vals.append(new_start)
        # Dauer erhalten, wenn das Ende nicht explizit mit verschoben wird (sonst läge es ggf. vor dem Start).
        if new_end is None and ev["end_ts"] is not None and not all_day:
            new_end = new_start + (ev["end_ts"] - ev["start_ts"])
    if new_end is not None:
        cols.append("end_ts=%s"); vals.append(new_end)
    if fields.get("all_day") is not None:
        cols.append("all_day=%s"); vals.append(all_day)
    if fields.get("recurrence") is not None:
        cols.append("rrule=%s"); vals.append(_RRULE.get(str(fields["recurrence"]).lower(), ""))
    if not cols:
        return False
    cols.append("updated_at=now()")
    with store._conn() as c:
        cur = c.execute(f"UPDATE calendar_events SET {', '.join(cols)} WHERE id=%s;", (*vals, event_id))
        return cur.rowcount > 0


def delete_event(event_id: int) -> bool:
    init()
    with store._conn() as c:
        cur = c.execute("DELETE FROM calendar_events WHERE id=%s;", (event_id,))
        return cur.rowcount > 0


def can_modify_event(user_id: int, ev: dict, is_admin: bool = False) -> bool:
    cal = get_calendar(ev["calendar_id"])
    return bool(is_admin or ev.get("created_by") == user_id or (cal and cal["owner_user_id"] == user_id))


def list_events(user_id: int, start: datetime, end: datetime, calendar_id: int | None = None) -> list[dict]:
    """Events im Zeitfenster über alle zugänglichen Kalender (oder einen bestimmten)."""
    cals = list_accessible(user_id)
    ids = [c["id"] for c in cals] if calendar_id is None else [calendar_id]
    ids = [i for i in ids if i in {c["id"] for c in cals}]      # nur zugängliche
    if not ids:
        return []
    names = {c["id"]: c["name"] for c in cals}
    with store._conn() as c:
        rows = c.execute(
            "SELECT id, calendar_id, title, location, start_ts, end_ts, all_day, rrule, created_by "
            "FROM calendar_events WHERE calendar_id = ANY(%s) AND start_ts < %s "
            "AND (rrule <> '' OR start_ts >= %s) ORDER BY start_ts;",
            (ids, end, start)).fetchall()
    out = []
    for r in rows:
        out.append({"id": r[0], "calendar_id": r[1], "calendar": names.get(r[1], "?"), "title": r[2],
                    "location": r[3], "start_ts": r[4], "end_ts": r[5], "all_day": r[6], "rrule": r[7],
                    "created_by": r[8]})
    return out


# ── Freigaben ─────────────────────────────────────────────────────────────────
def share(owner_user_id: int, grantee_user_id: int, access: str = "read") -> None:
    """Eigenen (persönlichen) Kalender für einen anderen Nutzer freigeben."""
    init()
    cal = ensure_user_calendar(owner_user_id)
    access = "write" if str(access).lower().startswith("w") else "read"
    with store._conn() as c:
        c.execute("INSERT INTO calendar_shares (calendar_id, user_id, access) VALUES (%s,%s,%s) "
                  "ON CONFLICT (calendar_id, user_id) DO UPDATE SET access=EXCLUDED.access;",
                  (cal["id"], grantee_user_id, access))


def unshare(owner_user_id: int, grantee_user_id: int) -> None:
    init()
    cal = ensure_user_calendar(owner_user_id)
    with store._conn() as c:
        c.execute("DELETE FROM calendar_shares WHERE calendar_id=%s AND user_id=%s;", (cal["id"], grantee_user_id))


def shares_of(owner_user_id: int) -> list[dict]:
    init()
    cal = ensure_user_calendar(owner_user_id)
    with store._conn() as c:
        rows = c.execute("SELECT user_id, access FROM calendar_shares WHERE calendar_id=%s;", (cal["id"],)).fetchall()
    return [{"user_id": r[0], "access": r[1]} for r in rows]


# ── iCal-Abo-Token ────────────────────────────────────────────────────────────
def user_token(user_id: int) -> str:
    init()
    with store._conn() as c:
        row = c.execute("SELECT token FROM calendar_user_token WHERE user_id=%s;", (user_id,)).fetchone()
        if row:
            return row[0]
        tok = _token()
        c.execute("INSERT INTO calendar_user_token (user_id, token) VALUES (%s,%s);", (user_id, tok))
    return tok


def calendar_by_token(token: str) -> dict | None:
    init()
    with store._conn() as c:
        row = c.execute("SELECT id, owner_user_id, name, kind, ics_token FROM calendars WHERE ics_token=%s;",
                        (token,)).fetchone()
    return _cal(row) if row else None


def user_by_token(token: str):
    init()
    with store._conn() as c:
        row = c.execute("SELECT user_id FROM calendar_user_token WHERE token=%s;", (token,)).fetchone()
    return row[0] if row else None


# ── iCal-Generierung (RFC 5545, minimal) ──────────────────────────────────────
def _esc(t: str) -> str:
    return (t or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _events_of(calendar_id: int) -> list[dict]:
    with store._conn() as c:
        rows = c.execute("SELECT id, calendar_id, uid, title, description, location, start_ts, end_ts, all_day, "
                         "rrule, created_at FROM calendar_events WHERE calendar_id=%s ORDER BY start_ts;",
                         (calendar_id,)).fetchall()
    return [{"uid": r[2], "title": r[3], "description": r[4], "location": r[5], "start_ts": r[6],
             "end_ts": r[7], "all_day": r[8], "rrule": r[9], "created_at": r[10]} for r in rows]


def _vevent(ev: dict) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{ev['uid']}",
             "DTSTAMP:" + (ev.get("created_at") or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")]
    s = ev["start_ts"]
    if ev["all_day"]:
        lines.append("DTSTART;VALUE=DATE:" + s.astimezone(LOCAL).strftime("%Y%m%d"))
        if ev["end_ts"]:
            lines.append("DTEND;VALUE=DATE:" + ev["end_ts"].astimezone(LOCAL).strftime("%Y%m%d"))
    else:
        lines.append("DTSTART:" + s.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        if ev["end_ts"]:
            lines.append("DTEND:" + ev["end_ts"].astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    lines.append("SUMMARY:" + _esc(ev["title"]))
    if ev.get("location"):
        lines.append("LOCATION:" + _esc(ev["location"]))
    if ev.get("description"):
        lines.append("DESCRIPTION:" + _esc(ev["description"]))
    if ev.get("rrule"):
        lines.append("RRULE:" + ev["rrule"])
    lines.append("END:VEVENT")
    return lines


def _ical(name: str, event_lists: list[list[dict]]) -> str:
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//JARVIS//Kalender//DE", "CALSCALE:GREGORIAN",
           "X-WR-CALNAME:" + _esc(name)]
    for evs in event_lists:
        for ev in evs:
            out += _vevent(ev)
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def ics_for_calendar(token: str) -> str | None:
    cal = calendar_by_token(token)
    if not cal:
        return None
    return _ical(cal["name"], [_events_of(cal["id"])])


def ics_for_user(token: str) -> str | None:
    uid = user_by_token(token)
    if uid is None:
        return None
    cals = list_accessible(uid)
    return _ical("JARVIS — alle Kalender", [_events_of(c["id"]) for c in cals])


# ── Externe iCal-Abos (eingehend: JARVIS kennt die Termine des Nutzers) ────────
def _unfold(text: str) -> list[str]:
    """RFC 5545 Line-Unfolding (Fortsetzungszeilen beginnen mit Space/Tab)."""
    out = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def _unesc(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _parse_ics_dt(val: str, params: dict):
    """ICS-Datum/Zeit → (aware UTC datetime, all_day bool)."""
    if params.get("VALUE") == "DATE" or (len(val) == 8 and val.isdigit()):
        d = datetime.strptime(val[:8], "%Y%m%d")
        return d.replace(tzinfo=LOCAL).astimezone(timezone.utc), True
    if val.endswith("Z"):
        return datetime.strptime(val, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
    dt = datetime.strptime(val[:15], "%Y%m%dT%H%M%S")
    tzid = params.get("TZID")
    tz = LOCAL
    if tzid:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tzid)
        except Exception:
            tz = LOCAL
    return dt.replace(tzinfo=tz).astimezone(timezone.utc), False


def parse_ics(text: str) -> list[dict]:
    """Externes ICS → Eventliste. Bewusst tolerant (Google/Nextcloud/iCloud-Feeds)."""
    events, cur = [], None
    for line in _unfold(text):
        u = line.strip()
        if u == "BEGIN:VEVENT":
            cur = {"title": "", "description": "", "location": "", "rrule": "", "all_day": False,
                   "start_ts": None, "end_ts": None, "uid": ""}
            continue
        if u == "END:VEVENT":
            if cur and cur["start_ts"] is not None:
                if not cur["uid"]:
                    cur["uid"] = secrets.token_hex(8) + "@ext"
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in u:
            continue
        head, _, val = u.partition(":")
        parts = head.split(";")
        key = parts[0].upper()
        params = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1); params[k.upper()] = v
        try:
            if key == "DTSTART":
                cur["start_ts"], cur["all_day"] = _parse_ics_dt(val, params)
            elif key == "DTEND":
                cur["end_ts"], _ = _parse_ics_dt(val, params)
            elif key == "SUMMARY":
                cur["title"] = _unesc(val)[:300]
            elif key == "LOCATION":
                cur["location"] = _unesc(val)[:300]
            elif key == "DESCRIPTION":
                cur["description"] = _unesc(val)[:1000]
            elif key == "RRULE":
                cur["rrule"] = val
            elif key == "UID":
                cur["uid"] = val[:200]
        except Exception:
            continue
    return events


def add_subscription(user_id: int, name: str, url: str) -> dict:
    """Externes iCal-Abo anlegen (read-only Kalender) und sofort synchronisieren."""
    init()
    with store._conn() as c:
        row = c.execute("INSERT INTO calendars (owner_user_id, name, kind, ics_token, source_url) "
                        "VALUES (%s,%s,'external',%s,%s) RETURNING id, owner_user_id, name, kind, ics_token;",
                        (user_id, name or "Externer Kalender", _token(), url)).fetchone()
    cal = _cal(row)
    n, err = sync_calendar(cal["id"])
    return {**cal, "synced": n, "error": err}


def list_subscriptions(user_id: int) -> list[dict]:
    init()
    with store._conn() as c:
        rows = c.execute("SELECT id, name, source_url, last_sync, last_error, "
                         "(SELECT COUNT(*) FROM calendar_events e WHERE e.calendar_id=c.id) "
                         "FROM calendars c WHERE owner_user_id=%s AND kind='external' ORDER BY name;",
                         (user_id,)).fetchall()
    return [{"id": r[0], "name": r[1], "url": r[2], "last_sync": str(r[3]) if r[3] else None,
             "last_error": r[4], "events": r[5]} for r in rows]


def remove_subscription(user_id: int, name_or_id: str) -> bool:
    init()
    with store._conn() as c:
        cur = c.execute("DELETE FROM calendars WHERE owner_user_id=%s AND kind='external' "
                        "AND (name ILIKE %s OR CAST(id AS TEXT)=%s);",
                        (user_id, name_or_id, str(name_or_id)))
        return cur.rowcount > 0


def sync_calendar(calendar_id: int) -> tuple[int, str | None]:
    """Externen Kalender abrufen + Events ersetzen. Gibt (Anzahl, Fehler) zurück."""
    init()
    with store._conn() as c:
        row = c.execute("SELECT source_url FROM calendars WHERE id=%s AND kind='external';",
                        (calendar_id,)).fetchone()
    if not row or not row[0]:
        return 0, "Kein externer Kalender."
    url = row[0]
    try:
        r = requests.get(url, timeout=20, verify=False)   # LAN-Feeds oft selbstsigniert
        r.raise_for_status()
        events = parse_ics(r.text)
    except Exception as e:
        with store._conn() as c:
            c.execute("UPDATE calendars SET last_error=%s WHERE id=%s;", (str(e)[:200], calendar_id))
        return 0, str(e)[:200]
    with store._conn() as c:
        c.execute("DELETE FROM calendar_events WHERE calendar_id=%s;", (calendar_id,))
        for ev in events:
            uid = (ev["uid"] or secrets.token_hex(8)) + f"#{calendar_id}"
            c.execute(
                "INSERT INTO calendar_events (calendar_id, uid, title, description, location, start_ts, end_ts, "
                "all_day, rrule, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL) ON CONFLICT (uid) DO NOTHING;",
                (calendar_id, uid, ev["title"] or "(ohne Titel)", ev["description"], ev["location"],
                 ev["start_ts"], ev["end_ts"], ev["all_day"], ev["rrule"]))
        c.execute("UPDATE calendars SET last_sync=now(), last_error=NULL WHERE id=%s;", (calendar_id,))
    return len(events), None


def sync_all() -> int:
    """Alle externen Abos synchronisieren (für den Hintergrund-Poller)."""
    init()
    with store._conn() as c:
        rows = c.execute("SELECT id FROM calendars WHERE kind='external';").fetchall()
    total = 0
    for (cid,) in rows:
        n, _ = sync_calendar(cid)
        total += n
    return total
