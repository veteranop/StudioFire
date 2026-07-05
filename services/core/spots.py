"""Spot rules — Station IDs, ads, jingles, PSAs inserted between songs.

A spot rule points at one of the configured station folders (Settings page)
and fires on a trigger; the file is chosen round-robin from that folder (the
same persisted-cursor rotation playlists use). Firing inserts the spot right
after the current song, so it airs at the next boundary — music is never cut.

Triggers:
  interval  — every interval_min minutes
  clock     — at clock_minutes past the hour (CSV, e.g. "0" or "20,40")
  once      — a single local 'YYYY-MM-DDTHH:MM' time, then disables itself
  daily     — every day at time_of_day ('HH:MM')
  weekly    — on the weekdays in days_mask (bit0=Mon..bit6=Sun) at time_of_day
  manual    — never auto-fires; the operator presses "Play now"

Recurring triggers (interval/clock/daily/weekly) honour an optional run window
[start_date .. end_date] — end_date is a "stop date" after which the spot stops
firing (e.g. an event promo that runs until August). daily/weekly fire once per
day (last_fired's date), with a short catch-up window for missed ticks.

This module is storage + timing math only. The feeder (engine_bridge) owns
resolving/precaching/injecting and calls due()/mark_fired().
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import time

# The station folders operators configure on the Settings page. Shared with
# app.py so the settings UI and the spot picker never drift apart.
#   (settings key, friendly label, hint)
FOLDER_CATEGORIES = [
    ("dir_shows", "Shows (syndicated)",
     "Downloaded/syndicated shows — 'newest from folder' items"),
    ("dir_ads", "Advertisements / spots",
     "Ad spots rotate evenly from here"),
    ("dir_station_ids", "Station IDs",
     "Legal IDs for the top of the hour"),
    ("dir_jingles", "Jingles / sweepers",
     "Short branding elements between songs"),
    ("dir_psas", "PSAs / liners",
     "Public service announcements and DJ liners"),
]
_LABELS = {k: lbl for k, lbl, _ in FOLDER_CATEGORIES}
TRIGGERS = ("interval", "clock", "once", "manual", "daily", "weekly")

# How late a daily/weekly slot may still fire (tolerates missed ticks / short
# outages), mirroring the On-Air schedule.
CATCH_UP_MIN = 20
_DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ------------------------------------------------------------------ helpers

def _parse_minutes(csv: str | None) -> list[int]:
    out = []
    for part in (csv or "").split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) <= 59:
            out.append(int(part))
    return sorted(set(out))


def _parse_dt(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return _dt.datetime.strptime(s[:16], "%Y-%m-%dT%H:%M").timestamp()
    except ValueError:
        return None


def default_label(folder_key: str) -> str:
    return _LABELS.get(folder_key, folder_key)


# --------------------------------------------------------------------- CRUD

def add(conn: sqlite3.Connection, folder_key: str, trigger: str,
        interval_min: int | None = None, clock_minutes: str | None = None,
        start_at: str | None = None, file_path: str | None = None,
        time_of_day: str | None = None, days_mask: int | None = None,
        start_date: str | None = None, end_date: str | None = None) -> int:
    """A spot rule targets either a folder (round-robin, folder_key) OR one
    specific file (file_path). file_path wins when set."""
    import os
    now = time.time()
    # interval rules start their clock at creation (first break N min later)
    last_fired = now if trigger == "interval" else None
    label = (os.path.splitext(os.path.basename(file_path))[0] if file_path
             else default_label(folder_key))
    with conn:
        cur = conn.execute(
            "INSERT INTO spot_rules (folder_key, label, trigger, interval_min, "
            "  clock_minutes, start_at, file_path, enabled, last_fired, "
            "  created_at, time_of_day, days_mask, start_date, end_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
            (folder_key or "", label, trigger, interval_min, clock_minutes,
             start_at, file_path, last_fired, now, time_of_day, days_mask,
             start_date or None, end_date or None))
    return cur.lastrowid


def remove(conn: sqlite3.Connection, rid: int) -> None:
    with conn:
        conn.execute("DELETE FROM spot_rules WHERE id = ?", (rid,))


def get(conn: sqlite3.Connection, rid: int) -> dict | None:
    row = conn.execute("SELECT * FROM spot_rules WHERE id = ?", (rid,)).fetchone()
    return dict(row) if row else None


def set_enabled(conn: sqlite3.Connection, rid: int, on: bool) -> None:
    with conn:
        conn.execute("UPDATE spot_rules SET enabled = ? WHERE id = ?",
                     (1 if on else 0, rid))


def list_all(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM spot_rules ORDER BY id").fetchall()]


def list_enabled(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM spot_rules WHERE enabled = 1 ORDER BY id").fetchall()]


def mark_fired(conn: sqlite3.Connection, rule: dict, now: float) -> None:
    with conn:
        if rule["trigger"] == "once":  # one-shot: don't fire again
            conn.execute("UPDATE spot_rules SET last_fired = ?, enabled = 0 "
                         "WHERE id = ?", (now, rule["id"]))
        else:
            conn.execute("UPDATE spot_rules SET last_fired = ? WHERE id = ?",
                         (now, rule["id"]))


# ------------------------------------------------------------------- timing

def _in_window(rule: dict, now: float) -> bool:
    """Is `now` inside the rule's optional run window [start_date, end_date]?"""
    today = _dt.date.fromtimestamp(now).isoformat()
    sd, ed = rule.get("start_date"), rule.get("end_date")
    if sd and today < sd:
        return False
    if ed and today > ed:
        return False
    return True


def _slot_due(rule: dict, now: float) -> bool:
    """daily/weekly: is the time-of-day slot due now (once per day)?"""
    tod = rule.get("time_of_day")
    if not tod:
        return False
    cur = _dt.datetime.fromtimestamp(now)
    lf = rule["last_fired"]
    if lf is not None and _dt.date.fromtimestamp(lf) == cur.date():
        return False  # already fired today
    if rule["trigger"] == "weekly":
        mask = rule.get("days_mask") or 0
        if not (mask & (1 << cur.weekday())):
            return False
    try:
        h, m = (int(x) for x in tod.split(":"))
        slot = cur.replace(hour=h, minute=m, second=0,
                           microsecond=0).timestamp()
    except (ValueError, TypeError):
        return False
    return 0 <= now - slot <= CATCH_UP_MIN * 60


def due(rule: dict, now: float) -> bool:
    """Should this rule fire right now?"""
    t = rule["trigger"]
    if not rule["enabled"] or t == "manual":
        return False
    if t != "once" and not _in_window(rule, now):
        return False  # outside the run window / past the stop date
    lf = rule["last_fired"]
    if t == "interval":
        n = max(1, rule["interval_min"] or 1) * 60
        base = lf if lf is not None else rule["created_at"]
        return now >= base + n
    if t == "clock":
        mins = _parse_minutes(rule["clock_minutes"])
        if not mins:
            return False
        cur = _dt.datetime.fromtimestamp(now)
        if cur.minute not in mins:
            return False
        minute_start = cur.replace(second=0, microsecond=0).timestamp()
        return lf is None or lf < minute_start  # not already fired this minute
    if t in ("daily", "weekly"):
        return _slot_due(rule, now)
    if t == "once":
        e = _parse_dt(rule["start_at"])
        return e is not None and now >= e
    return False


def next_fire_epoch(rule: dict, now: float) -> float | None:
    """When this rule will next fire (for the countdown display). None for
    manual/disabled rules."""
    t = rule["trigger"]
    if not rule["enabled"] or t == "manual":
        return None
    if t != "once" and rule.get("end_date"):
        # past the stop date it never fires again
        if _dt.date.fromtimestamp(now).isoformat() > rule["end_date"]:
            return None
    if t == "interval":
        n = max(1, rule["interval_min"] or 1) * 60
        base = rule["last_fired"] if rule["last_fired"] is not None \
            else rule["created_at"]
        nxt = base + n
        if nxt <= now:
            missed = int((now - base) // n) + 1
            nxt = base + missed * n
        return nxt
    if t == "clock":
        mins = _parse_minutes(rule["clock_minutes"])
        if not mins:
            return None
        cur = _dt.datetime.fromtimestamp(now).replace(second=0, microsecond=0)
        for i in range(0, 60 * 24 + 1):
            c = cur + _dt.timedelta(minutes=i)
            if c.minute in mins and c.timestamp() > now:
                return c.timestamp()
        return None
    if t in ("daily", "weekly"):
        tod = rule.get("time_of_day")
        if not tod:
            return None
        try:
            h, m = (int(x) for x in tod.split(":"))
        except (ValueError, TypeError):
            return None
        day = _dt.datetime.fromtimestamp(now).replace(
            hour=h, minute=m, second=0, microsecond=0)
        for i in range(0, 8):  # scan up to a week ahead for the next valid slot
            c = day + _dt.timedelta(days=i)
            if c.timestamp() <= now:
                continue
            if t == "weekly" and not ((rule.get("days_mask") or 0)
                                      & (1 << c.weekday())):
                continue
            if rule.get("end_date") and c.date().isoformat() > rule["end_date"]:
                return None
            return c.timestamp()
        return None
    if t == "once":
        return _parse_dt(rule["start_at"])
    return None


def _fmt_tod(tod: str | None) -> str:
    if not tod:
        return "?"
    try:
        return _dt.datetime.strptime(tod, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return tod


def describe(rule: dict) -> str:
    """Human summary of the trigger, e.g. 'every 15 min · until 2026-08-31'."""
    t = rule["trigger"]
    if t == "interval":
        base = f"every {rule['interval_min']} min"
    elif t == "clock":
        mins = _parse_minutes(rule["clock_minutes"])
        base = "at " + ", ".join(f":{m:02d}" for m in mins) + " past the hour"
    elif t == "once":
        return "once at " + (rule["start_at"] or "?").replace("T", " ")
    elif t == "daily":
        base = f"every day at {_fmt_tod(rule.get('time_of_day'))}"
    elif t == "weekly":
        mask = rule.get("days_mask") or 0
        days = ", ".join(_DAY_ABBR[i] for i in range(7) if mask & (1 << i)) \
            or "no days"
        base = f"{days} at {_fmt_tod(rule.get('time_of_day'))}"
    else:
        return "manual (Play now)"
    win = []
    if rule.get("start_date"):
        win.append("from " + rule["start_date"])
    if rule.get("end_date"):
        win.append("until " + rule["end_date"])
    return base + (" · " + ", ".join(win) if win else "")
