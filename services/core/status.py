"""One honest station-health verdict (release A, item 2).

The header pill used to read "healthy" straight off a pile of independent
tiles, so it could show green while the spot policy was empty or the engine
was not feeding — which is exactly how a 23-hour underwriting blackout went
unnoticed (2026-11-02). This module computes a SINGLE verdict a volunteer can
act on, in a fixed precedence (fault > warn > ok), and the pill is wired so it
can never read green when the verdict is not ok.

P2 only. Stdlib + existing P2 imports (db / spots / playlists). Never touches
P1 and never blocks the feeder — it is read-only against the DB and the status
snapshots P2 already fetches.

Design notes
------------
* Equipment pings are DELIBERATELY not part of the verdict. Elon's adjudication
  (CONSENSUS.md item 2): "ICMP equipment pings must never outrank audio + spot
  policy." A studio-LAN ping says nothing about whether audio is actually
  airing, and the capture artefacts proved it false-reds off the LAN. Equipment
  still shows in /api/health/tiles, just not here.
* Heartbeat staleness only faults when the heartbeat file EXISTS and is old.
  A missing file (common in dev where P2's data_dir != the live engine's) is
  not treated as a fault — engine reachability is the primary liveness signal.
* Sustained-silence duration is tracked across calls via a single settings
  key, so the alarm latches instead of needing someone to watch a meter.
"""

from __future__ import annotations

import os
import sqlite3

from . import db as coredb
from . import playlists as pl
from . import spots as spotmod

# Thresholds (seconds / dBFS).
HEARTBEAT_STALE_SEC = 30.0
SILENCE_DBFS = -55.0
SILENCE_WARN_SEC = 15.0
SILENCE_FAULT_SEC = 60.0
SKIP_WINDOW_MIN = 15

_SILENCE_KEY = "status_silence_since"   # epoch silence began, '' when not silent

_WORD = {"ok": "OK", "warn": "Check", "fault": "FAULT"}


def pill_word(state: str) -> str:
    return _WORD.get(state, "Check")


def _heartbeat_age(cfg: dict, now: float) -> float | None:
    """Age in seconds of the engine heartbeat file, or None if it's absent /
    unreadable (absence is not treated as a fault — see module docstring)."""
    path = cfg.get("heartbeat_path") or os.path.join(
        cfg.get("data_dir", "."), "engine_heartbeat.txt")
    try:
        with open(path, "r") as f:
            written = float((f.read() or "").strip())
    except (OSError, ValueError):
        return None
    return now - written


def _channels_silent(levels: dict) -> bool:
    """True when every program channel is at/under the silence floor. A None
    peak means the engine reported silence on that channel."""
    chans = levels.get("channels") or []
    if not chans:
        return False
    for ch in chans:
        peak = (ch or {}).get("peak")
        if peak is not None and peak > SILENCE_DBFS:
            return False
    return True


def _silence_seconds(conn, levels, paused: bool, engine_online: bool,
                     now: float) -> float:
    """Latched silence duration. Starts a clock when program audio goes quiet
    (engine up, not paused, metering on, all channels under the floor); resets
    it the moment audio returns, is paused, or the engine drops. Persisted in
    one settings key so the alarm survives page reloads / polling gaps."""
    silent = (engine_online and not paused and levels is not None
              and levels.get("enabled") is not False
              and _channels_silent(levels))
    raw = coredb.get_setting(conn, _SILENCE_KEY)
    try:
        since = float(raw) if raw else None
    except (TypeError, ValueError):
        since = None
    if not silent:
        if since is not None:
            coredb.set_setting(conn, _SILENCE_KEY, "")
        return 0.0
    if since is None:
        since = now
        coredb.set_setting(conn, _SILENCE_KEY, repr(now))
    return max(0.0, now - since)


def verdict(conn: sqlite3.Connection, cfg: dict,
            engine_status: dict | None, levels: dict | None,
            now: float) -> dict:
    """Compute the single station-health verdict.

    Returns {state, headline, reasons, silence_sec, checked} where state is
    'ok' | 'warn' | 'fault', reasons is a most-severe-first list of
    {state, text}, and checked carries the raw facts (for the UI / tests)."""
    es = engine_status or {}
    engine_online = engine_status is not None
    paused = bool(es.get("paused"))
    emergency = bool(es.get("emergency_mode"))

    hb_age = _heartbeat_age(cfg, now)
    silence_sec = _silence_seconds(conn, levels, paused, engine_online, now)

    enabled = spotmod.list_enabled(conn)
    n_enabled = len(enabled)
    legal = spotmod.legal_id_rules(conn)

    nas = cfg.get("nas_music_root") or ""
    nas_ok = (not nas) or os.path.isdir(pl.alias_path(nas))

    skipped_recent = _recent_skips(conn, now)

    faults: list[str] = []
    warns: list[str] = []

    # ---- faults (precedence order) ----
    if not engine_online:
        faults.append("Audio engine is not responding — check the on-air PC.")
    elif hb_age is not None and hb_age > HEARTBEAT_STALE_SEC:
        faults.append(f"Audio engine heartbeat is stale ({int(hb_age)}s) — "
                      "it may be wedged; check the on-air PC.")
    if emergency:
        faults.append("Emergency filler is on air — normal programming "
                      "stopped.")
    if paused:
        faults.append("Engine is paused — nothing is airing.")
    if n_enabled == 0:
        faults.append("No spot rules are enabled — nothing (IDs, ads, PSAs) "
                      "airs between songs.")
    if not legal:
        faults.append("No legal station-ID rule is enabled — the hourly ID "
                      "may be missed.")
    if silence_sec > SILENCE_FAULT_SEC:
        faults.append(f"Program audio has been silent for {int(silence_sec)}s "
                      "— the station may be off air.")

    # ---- warnings (only matter if there is no fault) ----
    if not nas_ok:
        warns.append("Music library (NAS) is unreachable — playing from the "
                     "local cache.")
    if SILENCE_WARN_SEC <= silence_sec <= SILENCE_FAULT_SEC:
        warns.append(f"Program audio has been quiet for {int(silence_sec)}s.")
    if skipped_recent:
        warns.append(f"{skipped_recent} playlist file(s) were missing and "
                     f"skipped in the last {SKIP_WINDOW_MIN} min.")

    reasons = ([{"state": "fault", "text": t} for t in faults]
               + [{"state": "warn", "text": t} for t in warns])
    if faults:
        state, headline = "fault", faults[0]
    elif warns:
        state, headline = "warn", warns[0]
    else:
        state = "ok"
        headline = "All monitored checks passed — audio, engine, spot policy."

    return {
        "state": state,
        "headline": headline,
        "reasons": reasons,
        "silence_sec": round(silence_sec, 1),
        "checked": {
            "engine_online": engine_online,
            "heartbeat_age": None if hb_age is None else round(hb_age, 1),
            "paused": paused,
            "emergency_mode": emergency,
            "enabled_spot_rules": n_enabled,
            "legal_id_rules": len(legal),
            "nas_ok": nas_ok,
            "silence_sec": round(silence_sec, 1),
            "skipped_last_15min": skipped_recent,
        },
    }


def _recent_skips(conn, now: float) -> int:
    """Count playlist items skipped-as-missing in the last SKIP_WINDOW_MIN.
    Guarded: the skipped_items table arrives in migration 14 (A4); until then
    (or if the query fails) this contributes nothing rather than crashing."""
    cutoff = now - SKIP_WINDOW_MIN * 60
    try:
        # DISTINCT path, not events: a playlist laps, so the same dead file is
        # skipped again every pass and the raw count inflates fast.
        row = conn.execute(
            "SELECT COUNT(DISTINCT path) FROM skipped_items WHERE ts >= ?",
            (cutoff,)).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0
