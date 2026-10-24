"""Now-playing text file — the title handoff to a source encoder (BUTT, Zara).

WHY THIS EXISTS
---------------
KDPI's stream encoder is **BUTT** running on a separate stream box. BUTT is
configured with `song_path = <file>` + `song_update = 1`: it polls a plain text
file and republishes whatever is in it as the ICY song title. That is the
station's original design (inherited from ZaraRadio) and it is the ONLY route
that works on their mount — StreamGuys' Icecast refuses `/admin/metadata`
URL updates with *"Mountpoint will not accept this URL update"* (measured
2026-10-24), so pushing over HTTP is not an option there.

So the contract is dead simple: **StudioFire writes the current title into that
file on every track change, and the encoder does the rest.**

Verified end to end on 2026-10-24: writing the file changed the live mount's
title within 10 seconds, and a listener's ICY block matched.

DESIGN LAWS
-----------
* **P2 only, feeder-thread safe.** The write happens in the feeder loop, so it
  must be cheap, bounded and must NEVER raise — a slow or missing network share
  must not stall the tick that also feeds P1's precache.
* **Atomic.** Write a temp file in the same directory then `os.replace`, so the
  encoder can never read a half-written title. SMB can fail the replace
  transiently, hence the small retry ladder (same shape as the precache
  manifest writer).
* **ASCII-folded.** The consumer chain is a legacy encoder into Icecast, which
  was measured mangling UTF-8 (`Björk` → `BjÃ¶rk`). Folding keeps what renders
  correct; a missing umlaut beats mojibake on a live station.
* **Independent of the HTTP push.** This works with `nowplaying_push.enabled`
  false — which is the configuration the station actually wants.
"""

from __future__ import annotations

import logging
import ntpath
import os
import time
import unicodedata

from . import db

log = logging.getLogger("core.nowplaying_file")

# DB setting holding the file path (empty = feature off)
K_PATH = "stream_np_file"

# Sources that count as "a song is on air". Spots / IDs / PSAs deliberately do
# not overwrite the title — listeners should keep seeing the track, and the
# regulation-facing logs are the source of truth for what aired.
MUSIC_SOURCES = ("playlist", "show", "manual", "emergency")

# last write, for the Settings monitor (read by another thread; a stale read is
# always fine for a display)
LAST = {"path": None, "title": None, "at": None, "ok": None, "error": None}


def get_path(conn) -> str:
    return (db.get_setting(conn, K_PATH) or "").strip()


def set_path(conn, path: str) -> None:
    db.set_setting(conn, K_PATH, (path or "").strip())


def fold(text: str) -> str:
    """ASCII-fold, keeping the artist/title dash. See the module docstring."""
    try:
        t = text or ""
        for dash in ("\u2014", "\u2013"):
            t = t.replace(dash, "-")
        t = unicodedata.normalize("NFKD", t)
        t = t.encode("ascii", "ignore").decode("ascii")
        return " ".join(t.split())
    except Exception:
        return (text or "").encode("ascii", "ignore").decode("ascii").strip()


def choose(events, now_playing: str | None = None):
    """Pick the event that is actually on air right now.

    `events` are the journal `track_start` events just ingested. Prefer the one
    whose path matches the engine's live `now_playing` (a backlog drain after a
    P2 restart can deliver several at once); otherwise the newest. Spots and
    anything without a title are skipped.
    """
    usable = [e for e in (events or [])
              if e.get("title") and e.get("source") in MUSIC_SOURCES]
    if not usable:
        return None
    if now_playing:
        for e in usable:
            if e.get("path") == now_playing:
                return e
    return max(usable, key=lambda e: str(e.get("id") or ""))


def _atomic_write(path: str, text: str) -> None:
    """Write `text` to `path` via temp+replace, with a short retry ladder for
    the transient SMB/AV locks Windows throws at `os.replace`."""
    folder = ntpath.dirname(path) or "."
    tmp = os.path.join(folder, ".sf-nowplaying.tmp")
    with open(tmp, "w", encoding="ascii", newline="\n") as fh:
        fh.write(text + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    last = None
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except OSError as exc:          # noqa: PERF203 - tiny ladder, deliberate
            last = exc
            time.sleep(0.05 * (attempt + 1))
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last if last else OSError("replace failed")


def write_text(path: str, title: str) -> bool:
    """Write one title to the file. Never raises; records the outcome in LAST."""
    title = fold(title)
    if not path or not title:
        LAST.update(path=path or None, title=title or None, ok=False,
                    at=time.strftime("%Y-%m-%d %H:%M:%S"),
                    error=None if not title else "no path configured")
        return False
    try:
        _atomic_write(path, title)
    except Exception as exc:
        LAST.update(path=path, title=title, ok=False,
                    at=time.strftime("%Y-%m-%d %H:%M:%S"), error=str(exc))
        log.warning("now-playing file write failed (%s): %s", path, exc)
        return False
    LAST.update(path=path, title=title, ok=True,
                at=time.strftime("%Y-%m-%d %H:%M:%S"), error=None)
    log.info("now-playing file <- %r", title)
    return True


def write_now_playing(conn, events, now_playing: str | None = None) -> bool:
    """Called from the feeder loop after each journal ingest. No-op unless a
    path is configured. Returns True when a title was written."""
    try:
        path = get_path(conn)
        if not path:
            return False
        ev = choose(events, now_playing)
        if ev is None:
            return False
        return write_text(path, ev.get("title") or "")
    except Exception:
        log.warning("now-playing file step failed (non-fatal)", exc_info=True)
        return False


def write_current(conn, engine_status=None) -> bool:
    """Write whatever is on air NOW from the play history — used at startup so a
    restarted P2 does not leave a stale title behind. Never raises."""
    try:
        path = get_path(conn)
        if not path:
            return False
        marks = ",".join("?" * len(MUSIC_SOURCES))
        row = conn.execute(
            "SELECT title, path FROM play_history WHERE event = 'track_start' "
            f"AND source IN ({marks}) AND title IS NOT NULL AND title <> '' "
            "ORDER BY id DESC LIMIT 1", MUSIC_SOURCES).fetchone()
        if row is None:
            return False
        now_playing = None
        try:
            now_playing = (engine_status or {}).get("now_playing")
        except Exception:
            now_playing = None
        if now_playing and row["path"] != now_playing:
            # the history row is the best we have; still better than stale
            pass
        return write_text(path, row["title"])
    except Exception:
        log.warning("now-playing file startup write failed (non-fatal)",
                    exc_info=True)
        return False


def status() -> dict:
    """For the Settings > Streaming monitor."""
    return dict(LAST)
