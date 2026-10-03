"""On-Air schedule — cued/scheduled "shows" that interrupt the rotation.

A show plays ONCE through, then hands back to the base rotation
(active_playlist_id). A show's source can be:
  - playlist : a StudioFire playlist (playlist_id)
  - file     : a single audio file (source_path) — e.g. a whole show as one mp3
  - lst      : a ZaraRadio .lst file (source_path), read live at air time

Timing (recurrence):
  - once   : fires when start_at ('YYYY-MM-DDTHH:MM' local) arrives, then done;
             start_at NULL = manual cue (operator presses Start now / Cue next).
  - daily  : fires every day at time_of_day ('HH:MM'), within [start_date, end_date].
  - weekly : fires on the weekdays in days_mask (bit0=Mon..bit6=Sun) at time_of_day,
             within [start_date, end_date].
Recurring rows re-arm (state -> waiting) after each airing instead of going
'done'; last_fired ('YYYY-MM-DD') keeps a slot to one airing per day. A "stop
date" is just end_date — past it the row is retired (state -> done).

The feeder owns state transitions; this module is storage + queries only.
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import time

# How late a recurring slot may still fire (tolerates missed ticks / short
# outages / the up-to-one-song boundary delay) before it's skipped for the day.
CATCH_UP_MIN = 20

_SEL = ("SELECT s.*, p.name AS playlist_name FROM playlist_schedule s "
        "LEFT JOIN playlists p ON p.id = s.playlist_id ")

_DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def now_local() -> str:
    """Local wall-clock in the same format one-time schedules are stored in."""
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M")


# Clock display preference, set from the station's saved setting (app.py sets
# this at startup and whenever the operator changes it on the Settings page) so
# the server-rendered 'when'/'next' labels match the header clock. True =
# 12-hour (AM/PM); False = 24-hour (military).
_HOUR12 = True


def hour12() -> bool:
    """True when clock times should be shown as 12-hour (AM/PM)."""
    return _HOUR12


def set_hour12(flag: bool) -> None:
    """Set the clock display preference. Called by app.py at startup and when
    the operator switches the format in Settings."""
    global _HOUR12
    _HOUR12 = bool(flag)


def _fmt_tod(tod: str | None) -> str:
    """'18:30' -> '6:30 PM' in 12-hour mode, or '18:30' in 24-hour mode."""
    if not tod:
        return ""
    try:
        dt = _dt.datetime.strptime(tod, "%H:%M")
    except (ValueError, TypeError):
        return tod
    if not _HOUR12:
        return dt.strftime("%H:%M")
    try:
        return dt.strftime("%-I:%M %p")
    except ValueError:
        try:  # Windows strftime has no %-I
            return dt.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            return tod


def _fmt_dt(s: str | None) -> str:
    """A stored 'YYYY-MM-DDTHH:MM' timestamp -> plain English: the date plus the
    time in the station's chosen format ('2026-10-03 6:30 PM' / '18:30')."""
    if not s:
        return ""
    try:
        d = _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M")
    except (ValueError, TypeError):
        return s.replace("T", " ")
    return d.strftime("%Y-%m-%d ") + _fmt_tod(d.strftime("%H:%M"))


def _when_label(r: dict) -> str:
    """Human 'when' text for the upcoming list."""
    rec = r.get("recurrence") or "once"
    if rec == "once":
        base = "Manual — press Start now" if not r.get("start_at") \
            else _fmt_dt(r["start_at"])
        return base
    if rec == "daily":
        base = f"Every day at {_fmt_tod(r.get('time_of_day'))}"
    else:  # weekly
        mask = r.get("days_mask") or 0
        days = ", ".join(_DAY_ABBR[i] for i in range(7) if mask & (1 << i)) \
            or "no days"
        base = f"{days} at {_fmt_tod(r.get('time_of_day'))}"
    win = []
    if r.get("start_date"):
        win.append("from " + r["start_date"])
    if r.get("end_date"):
        win.append("until " + r["end_date"])
    return base + (" · " + ", ".join(win) if win else "")


def _display(row) -> dict:
    """Add a human `name` (from the source kind) and `when` label."""
    r = dict(row)
    if r.get("source_kind", "playlist") == "playlist":
        r["name"] = r.get("playlist_name") or "(deleted playlist)"
    else:  # file / lst -> the file's name
        p = r.get("source_path") or ""
        r["name"] = os.path.splitext(os.path.basename(p))[0] or p or "(no file)"
    r["when"] = _when_label(r)
    return r


def add(conn: sqlite3.Connection, source_kind: str = "playlist",
        playlist_id: int | None = None, source_path: str | None = None,
        start_at: str | None = None, recurrence: str = "once",
        time_of_day: str | None = None, days_mask: int | None = None,
        start_date: str | None = None, end_date: str | None = None) -> int:
    with conn:
        sort = conn.execute(
            "SELECT COALESCE(MAX(sort) + 1, 0) FROM playlist_schedule"
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO playlist_schedule (playlist_id, source_kind, "
            "  source_path, start_at, sort, state, created_at, recurrence, "
            "  time_of_day, days_mask, start_date, end_date) "
            "VALUES (?, ?, ?, ?, ?, 'waiting', ?, ?, ?, ?, ?, ?)",
            (playlist_id, source_kind, source_path, start_at or None, sort,
             time.time(), recurrence, time_of_day, days_mask,
             start_date or None, end_date or None))
    return cur.lastrowid


def remove(conn: sqlite3.Connection, sid: int) -> None:
    with conn:
        conn.execute("DELETE FROM playlist_schedule WHERE id = ?", (sid,))


def get(conn: sqlite3.Connection, sid: int) -> dict | None:
    row = conn.execute(_SEL + "WHERE s.id = ?", (sid,)).fetchone()
    return _display(row) if row else None


def set_state(conn: sqlite3.Connection, sid: int, state: str) -> None:
    with conn:
        conn.execute("UPDATE playlist_schedule SET state = ? WHERE id = ?",
                     (state, sid))


def mark_fired(conn: sqlite3.Connection, sid: int, day: str) -> None:
    """Record that a recurring slot aired today so it won't re-fire today."""
    with conn:
        conn.execute("UPDATE playlist_schedule SET last_fired = ? WHERE id = ?",
                     (day, sid))


def finish(conn: sqlite3.Connection, sid: int, today: str | None = None) -> None:
    """A show finished airing: retire a one-time show (done); re-arm a
    recurring one (back to waiting) unless its run window has ended."""
    row = conn.execute("SELECT recurrence, end_date FROM playlist_schedule "
                       "WHERE id = ?", (sid,)).fetchone()
    if row is None:
        return
    today = today or _dt.date.today().isoformat()
    recurring = (row["recurrence"] or "once") != "once"
    expired = bool(row["end_date"]) and today > row["end_date"]
    set_state(conn, sid, "waiting" if (recurring and not expired) else "done")


def finish_all_playing(conn: sqlite3.Connection,
                       except_id: int | None = None) -> int:
    """Retire/re-arm every row marked 'playing' (except `except_id`). Exactly
    one show is ever on air, but the feeder's overlay can drift out of sync with
    these state flags (crashes, restarts, two Start-nows) leaving a stale
    'playing' row — which then wrongly shows as SHOW ON AIR. Called whenever a
    show starts, so a fresh start always supersedes any strays. Returns count."""
    rows = conn.execute("SELECT id FROM playlist_schedule "
                        "WHERE state = 'playing'").fetchall()
    n = 0
    for r in rows:
        if except_id is not None and r["id"] == except_id:
            continue
        finish(conn, r["id"])
        n += 1
    return n


def expire_past(conn: sqlite3.Connection, today: str | None = None) -> int:
    """Retire recurring rows whose end_date has passed so they drop off the
    upcoming list. Returns how many were retired."""
    today = today or _dt.date.today().isoformat()
    with conn:
        cur = conn.execute(
            "UPDATE playlist_schedule SET state = 'done' "
            "WHERE state = 'waiting' AND recurrence != 'once' "
            "AND end_date IS NOT NULL AND end_date < ?", (today,))
    return cur.rowcount


def next_occurrence(r: dict,
                    now: _dt.datetime | None = None) -> _dt.datetime | None:
    """When this schedule row will next air, or None (manual cue / past its
    stop date / no valid time). A recurring slot that's inside its catch-up
    window and hasn't aired today counts as 'now' (it's about to fire)."""
    now = now or _dt.datetime.now()
    rec = r.get("recurrence") or "once"
    if rec == "once":
        try:
            return _dt.datetime.strptime(r["start_at"][:16], "%Y-%m-%dT%H:%M") \
                if r.get("start_at") else None
        except ValueError:
            return None
    try:
        tod = _dt.datetime.strptime(r.get("time_of_day") or "", "%H:%M").time()
    except ValueError:
        return None
    day = now.date()
    if r.get("start_date"):
        try:
            day = max(day, _dt.date.fromisoformat(r["start_date"]))
        except ValueError:
            pass
    end = None
    if r.get("end_date"):
        try:
            end = _dt.date.fromisoformat(r["end_date"])
        except ValueError:
            pass
    mask = r.get("days_mask") or 0
    for _ in range(8):  # a weekly slot recurs within 7 days of `day`
        if end and day > end:
            return None
        slot = _dt.datetime.combine(day, tod)
        if ((rec == "daily" or mask & (1 << day.weekday()))
                and r.get("last_fired") != day.isoformat()
                and (now - slot).total_seconds() <= CATCH_UP_MIN * 60):
            return slot
        day += _dt.timedelta(days=1)
    return None


def next_label(when: _dt.datetime | None,
               now: _dt.datetime | None = None) -> str:
    """'Today 6:00 AM' / 'Tomorrow 6:00 AM' / 'Sat Sep 27, 6:00 AM'."""
    if when is None:
        return ""
    now = now or _dt.datetime.now()
    tod = _fmt_tod(when.strftime("%H:%M"))
    delta = (when.date() - now.date()).days
    if delta <= 0:
        return f"Today {tod}"
    if delta == 1:
        return f"Tomorrow {tod}"
    return f"{when:%a %b} {when.day}, {tod}"


def list_waiting(conn: sqlite3.Connection,
                 now: _dt.datetime | None = None) -> list[dict]:
    """Upcoming shows in the order they'll actually air (soonest first, one-
    time and recurring interleaved), then anything with no next airing
    (manual cues) in the order it was added. Each row gains `next_at`
    ('YYYY-MM-DDTHH:MM' or None) and a plain-English `next_label`."""
    now = now or _dt.datetime.now()
    rows = conn.execute(
        _SEL + "WHERE s.state = 'waiting' ORDER BY s.sort").fetchall()
    out = []
    for row in rows:
        r = _display(row)
        nxt = next_occurrence(r, now)
        r["next_at"] = nxt.strftime("%Y-%m-%dT%H:%M") if nxt else None
        r["next_label"] = next_label(nxt, now)
        out.append(r)
    out.sort(key=lambda r: (r["next_at"] is None, r["next_at"] or ""))
    return out


def playing(conn: sqlite3.Connection) -> dict | None:
    """The show currently on air (if any)."""
    row = conn.execute(
        _SEL + "WHERE s.state = 'playing' ORDER BY s.id LIMIT 1").fetchone()
    return _display(row) if row else None


def _recurring_due(r: dict, now_dt: _dt.datetime) -> bool:
    """Is this recurring row due to fire right now?"""
    tod = r.get("time_of_day")
    if not tod:
        return False
    today = now_dt.strftime("%Y-%m-%d")
    if r.get("last_fired") == today:
        return False                       # already aired this slot today
    if r.get("start_date") and today < r["start_date"]:
        return False                       # run window hasn't opened
    if r.get("end_date") and today > r["end_date"]:
        return False                       # past the stop date
    if (r.get("recurrence") or "once") == "weekly":
        mask = r.get("days_mask") or 0
        if not (mask & (1 << now_dt.weekday())):
            return False                   # not one of the chosen weekdays
    try:
        slot = _dt.datetime.strptime(today + " " + tod, "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    delta = (now_dt - slot).total_seconds()
    return 0 <= delta <= CATCH_UP_MIN * 60


def occurrences_on(conn: sqlite3.Connection, date: _dt.date) -> list[dict]:
    """Every scheduled show that airs on `date` (a datetime.date), for the
    calendar view: [{time, name, source_kind, recurrence}], sorted by time.
    Recurring shows resolve against their weekday/run-window; one-shots match
    their start_at date. 'done' one-shots are excluded."""
    iso = date.isoformat()
    out = []
    for row in conn.execute(_SEL + "WHERE s.state != 'done'").fetchall():
        r = _display(row)
        rec = r.get("recurrence") or "once"
        if rec == "once":
            sa = r.get("start_at")
            if sa and sa[:10] == iso:
                out.append({"time": sa[11:16], "name": r["name"],
                            "source_kind": r["source_kind"],
                            "recurrence": "once"})
            continue
        sd, ed = r.get("start_date"), r.get("end_date")
        if (sd and iso < sd) or (ed and iso > ed):
            continue
        if rec == "weekly" and not ((r.get("days_mask") or 0)
                                    & (1 << date.weekday())):
            continue
        out.append({"time": r.get("time_of_day") or "", "name": r["name"],
                    "source_kind": r["source_kind"], "recurrence": rec})
    out.sort(key=lambda x: x["time"] or "99:99")
    return out


def due(conn: sqlite3.Connection,
        now: _dt.datetime | None = None) -> dict | None:
    """The next waiting show whose time has arrived — recurring slots first,
    then one-time (start_at) shows."""
    now_dt = now or _dt.datetime.now()
    rows = conn.execute(_SEL + "WHERE s.state = 'waiting'").fetchall()
    for row in rows:                       # recurring: time-sensitive, first
        r = dict(row)
        if (r.get("recurrence") or "once") != "once":
            if _recurring_due(r, now_dt):
                return _display(row)
    now_str = now_dt.strftime("%Y-%m-%dT%H:%M")
    best = None
    for row in rows:                       # one-time: earliest due start_at
        r = dict(row)
        if (r.get("recurrence") or "once") != "once":
            continue
        if r.get("start_at") and r["start_at"] <= now_str:
            if best is None or r["start_at"] < best["start_at"]:
                best = r
    return _display(best) if best else None
