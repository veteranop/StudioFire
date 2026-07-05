"""On-Air schedule — cued/scheduled "shows" that interrupt the rotation.

A show plays ONCE through, then hands back to the base rotation
(active_playlist_id). A show's source can be:
  - playlist : a StudioFire playlist (playlist_id)
  - file     : a single audio file (source_path) — e.g. a whole show as one mp3
  - lst      : a ZaraRadio .lst file (source_path), read live at air time

Timing: scheduled (start_at = 'YYYY-MM-DDTHH:MM' local) or manual (start_at
NULL, operator presses Start now / Cue next). State: waiting -> playing -> done.
The feeder owns transitions; this module is storage + queries only.
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import time

_SEL = ("SELECT s.*, p.name AS playlist_name FROM playlist_schedule s "
        "LEFT JOIN playlists p ON p.id = s.playlist_id ")


def now_local() -> str:
    """Local wall-clock in the same format schedules are stored in."""
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M")


def _display(row) -> dict:
    """Add a human `name` derived from the source kind."""
    r = dict(row)
    if r.get("source_kind", "playlist") == "playlist":
        r["name"] = r.get("playlist_name") or "(deleted playlist)"
    else:  # file / lst -> the file's name
        p = r.get("source_path") or ""
        r["name"] = os.path.splitext(os.path.basename(p))[0] or p or "(no file)"
    return r


def add(conn: sqlite3.Connection, source_kind: str = "playlist",
        playlist_id: int | None = None, source_path: str | None = None,
        start_at: str | None = None) -> int:
    with conn:
        sort = conn.execute(
            "SELECT COALESCE(MAX(sort) + 1, 0) FROM playlist_schedule"
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO playlist_schedule (playlist_id, source_kind, "
            "  source_path, start_at, sort, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'waiting', ?)",
            (playlist_id, source_kind, source_path, start_at or None, sort,
             time.time()))
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


def list_waiting(conn: sqlite3.Connection) -> list[dict]:
    """Upcoming shows: timed ones first (by time), then manual cues."""
    rows = conn.execute(
        _SEL + "WHERE s.state = 'waiting' "
        "ORDER BY (s.start_at IS NULL), s.start_at, s.sort").fetchall()
    return [_display(r) for r in rows]


def playing(conn: sqlite3.Connection) -> dict | None:
    """The show currently on air (if any)."""
    row = conn.execute(
        _SEL + "WHERE s.state = 'playing' ORDER BY s.id LIMIT 1").fetchone()
    return _display(row) if row else None


def due(conn: sqlite3.Connection, now: str | None = None) -> dict | None:
    """The earliest waiting *scheduled* show whose time has arrived."""
    now = now or now_local()
    row = conn.execute(
        _SEL + "WHERE s.state = 'waiting' AND s.start_at IS NOT NULL "
        "AND s.start_at <= ? ORDER BY s.start_at LIMIT 1", (now,)).fetchone()
    return _display(row) if row else None
