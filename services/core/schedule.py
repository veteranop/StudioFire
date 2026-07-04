"""Playlist schedule — cued/scheduled "shows" that interrupt the rotation.

A show is a playlist that plays ONCE through, then hands back to the base
rotation (the active_playlist_id). Two ways in (user chose "Both"):
  - scheduled: start_at = 'YYYY-MM-DDTHH:MM' local; the feeder fires it when
    that time arrives, at the next song boundary.
  - manual cue: start_at = NULL; the operator presses "Start now".

State machine: waiting -> playing (on fire) -> done (when its items run out).
The feeder owns the transitions; this module is just storage + queries.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import time


def now_local() -> str:
    """Local wall-clock in the same format schedules are stored in."""
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M")


def add(conn: sqlite3.Connection, playlist_id: int,
        start_at: str | None = None) -> int:
    with conn:
        sort = conn.execute(
            "SELECT COALESCE(MAX(sort) + 1, 0) FROM playlist_schedule"
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO playlist_schedule "
            "  (playlist_id, start_at, sort, state, created_at) "
            "VALUES (?, ?, ?, 'waiting', ?)",
            (playlist_id, start_at or None, sort, time.time()))
    return cur.lastrowid


def remove(conn: sqlite3.Connection, sid: int) -> None:
    with conn:
        conn.execute("DELETE FROM playlist_schedule WHERE id = ?", (sid,))


def get(conn: sqlite3.Connection, sid: int) -> dict | None:
    row = conn.execute(
        "SELECT s.*, p.name AS playlist_name "
        "FROM playlist_schedule s JOIN playlists p ON p.id = s.playlist_id "
        "WHERE s.id = ?", (sid,)).fetchone()
    return dict(row) if row else None


def set_state(conn: sqlite3.Connection, sid: int, state: str) -> None:
    with conn:
        conn.execute("UPDATE playlist_schedule SET state = ? WHERE id = ?",
                     (state, sid))


def list_waiting(conn: sqlite3.Connection) -> list[dict]:
    """Upcoming shows for the On Air panel: timed ones first (by time),
    then manual cues (by the order they were added)."""
    rows = conn.execute(
        "SELECT s.*, p.name AS playlist_name "
        "FROM playlist_schedule s JOIN playlists p ON p.id = s.playlist_id "
        "WHERE s.state = 'waiting' "
        "ORDER BY (s.start_at IS NULL), s.start_at, s.sort").fetchall()
    return [dict(r) for r in rows]


def playing(conn: sqlite3.Connection) -> dict | None:
    """The show currently on air (if any)."""
    row = conn.execute(
        "SELECT s.*, p.name AS playlist_name "
        "FROM playlist_schedule s JOIN playlists p ON p.id = s.playlist_id "
        "WHERE s.state = 'playing' ORDER BY s.id LIMIT 1").fetchone()
    return dict(row) if row else None


def due(conn: sqlite3.Connection, now: str | None = None) -> dict | None:
    """The earliest waiting *scheduled* show whose time has arrived."""
    now = now or now_local()
    row = conn.execute(
        "SELECT s.*, p.name AS playlist_name "
        "FROM playlist_schedule s JOIN playlists p ON p.id = s.playlist_id "
        "WHERE s.state = 'waiting' AND s.start_at IS NOT NULL "
        "  AND s.start_at <= ? "
        "ORDER BY s.start_at LIMIT 1", (now,)).fetchone()
    return dict(row) if row else None
