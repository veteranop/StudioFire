"""P3 library indexer — incremental NAS scan into SQLite (PLAN.md §6 Phase 1).

Incremental by design: a file is only tag-read (the expensive part, over SMB)
when it is new or its size/mtime changed. Unchanged files cost one stat call.
Never full-rescan-by-default — 4TB would hammer both the NAS and this PC.

Progress/status is written to the settings table (key 'indexer_status') so
P2 can render an "indexing… N scanned" tile without any coupling.

Throttled: a short sleep every THROTTLE_EVERY files keeps CPU/SMB load down
(this runs on the on-air PC — playback always wins).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time

import mutagen

from services.core import db as coredb

log = logging.getLogger("worker.indexer")

AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".aac", ".wav", ".flac", ".ogg"}
THROTTLE_EVERY = 200      # files between throttle naps
THROTTLE_NAP = 0.05       # seconds


def read_tags(path: str) -> dict:
    """Best-effort tag read; a corrupt file must never kill the scan."""
    out = {"title": None, "artist": None, "album": None,
           "duration_sec": None, "format": None}
    try:
        m = mutagen.File(path, easy=True)
    except Exception:  # noqa: BLE001 — mutagen raises wildly varied errors
        return out
    if m is None:
        return out
    if m.info is not None:
        out["duration_sec"] = round(getattr(m.info, "length", 0.0) or 0.0, 2)
    out["format"] = type(m).__name__
    if m.tags:
        def first(key):
            v = m.tags.get(key)
            return str(v[0]) if v else None
        out["title"] = first("title")
        out["artist"] = first("artist")
        out["album"] = first("album")
    if not out["title"]:
        out["title"] = os.path.splitext(os.path.basename(path))[0]
    return out


def _write_status(conn: sqlite3.Connection, **kv) -> None:
    coredb.set_setting(conn, "indexer_status", json.dumps(kv))


def scan(conn: sqlite3.Connection, root: str,
         stop_check=lambda: False) -> dict:
    """One incremental pass. Returns counters."""
    t0 = time.time()
    stats = {"scanned": 0, "added": 0, "updated": 0, "missing": 0,
             "errors": 0}
    _write_status(conn, state="scanning", root=root, started_at=t0, **stats)

    known = {row["path"]: (row["mtime"], row["size"])
             for row in conn.execute("SELECT path, mtime, size FROM tracks")}
    seen: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if stop_check():
            break
        for name in filenames:
            if os.path.splitext(name)[1].lower() not in AUDIO_EXTS:
                continue
            path = os.path.join(dirpath, name)
            stats["scanned"] += 1
            if stats["scanned"] % THROTTLE_EVERY == 0:
                time.sleep(THROTTLE_NAP)
                _write_status(conn, state="scanning", root=root,
                              started_at=t0, **stats)
                if stop_check():
                    break
            try:
                st = os.stat(path)
            except OSError:
                stats["errors"] += 1
                continue
            seen.add(path)
            prev = known.get(path)
            if prev is not None and prev == (st.st_mtime, st.st_size):
                continue  # unchanged — the incremental fast path
            tags = read_tags(path)
            with conn:
                conn.execute(
                    "INSERT INTO tracks (path, title, artist, album, "
                    "  duration_sec, format, size, mtime, indexed_at, "
                    "  missing) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    "  title = excluded.title, artist = excluded.artist, "
                    "  album = excluded.album, "
                    "  duration_sec = excluded.duration_sec, "
                    "  format = excluded.format, size = excluded.size, "
                    "  mtime = excluded.mtime, "
                    "  indexed_at = excluded.indexed_at, missing = 0",
                    (path, tags["title"], tags["artist"], tags["album"],
                     tags["duration_sec"], tags["format"],
                     st.st_size, st.st_mtime, time.time()))
            stats["added" if prev is None else "updated"] += 1

    # flag rows whose files vanished (don't delete — playlists may reference)
    gone = [p for p in known if p not in seen and p.startswith(
        os.path.join(root, ""))]
    if gone and not stop_check():
        with conn:
            for p in gone:
                conn.execute("UPDATE tracks SET missing = 1 WHERE path = ?",
                             (p,))
        stats["missing"] = len(gone)

    _write_status(conn, state="idle", root=root, started_at=t0,
                  finished_at=time.time(), **stats)
    log.info("scan done in %.1fs: %s", time.time() - t0, stats)
    return stats
