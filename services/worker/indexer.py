"""P3 library indexer — incremental NAS scan into SQLite (PLAN.md §6 Phase 1).

Two phases per pass, so search comes alive fast on a cold 4TB library:
  PHASE 1 (paths): os.walk + stat only, no file opens. New/changed files are
    recorded with a filename title and tags_read=0. This is cheap (one stat per
    file) so the WHOLE library becomes searchable in minutes, not hours.
  PHASE 2 (tags): backfill artist/album/duration for tags_read=0 rows by
    actually opening each file with mutagen. This is the expensive part — a VBR
    MP3 with no header forces mutagen to read the whole file over SMB (~seconds
    each) — but it runs AFTER search already works, enriching rows in place.

Incremental by design: an unchanged file costs one stat and is skipped; only
new/changed files are (re)tagged. Never full-rescan-by-default — 4TB would
hammer both the NAS and this PC.

Progress/status is written to the settings table (key 'indexer_status') so
P2 can render an "indexing… N tracks" tile without any coupling.

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
TAG_BATCH = 100           # rows fetched per tag-backfill batch


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


def _walk_paths(conn: sqlite3.Connection, root: str, t0: float,
                stop_check) -> tuple[dict, set, set]:
    """PHASE 1: record every audio file's PATH (stat only, no tag reads) so the
    library is searchable fast. New/changed files get a filename title and
    tags_read=0; PHASE 2 fills real tags later. Returns (stats, seen, dirs)."""
    stats = {"scanned": 0, "added": 0, "updated": 0, "missing": 0, "errors": 0}
    known = {row["path"]: (row["mtime"], row["size"])
             for row in conn.execute("SELECT path, mtime, size FROM tracks")}
    seen: set[str] = set()
    walked_dirs: set[str] = set()  # folders we could actually read

    # Iterative DFS with os.scandir instead of os.walk + per-file os.stat. On
    # Windows the directory enumeration already carries each entry's size and
    # mtime, so DirEntry.stat() is FREE (no extra SMB round-trip per file) —
    # critical when the NAS takes ~30ms per metadata op. Sorted so search fills
    # in alphabetically (A->Z), same as the old os.walk.
    stack = [root]
    while stack and not stop_check():
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue  # unreadable folder — leave its known rows untouched
        walked_dirs.add(os.path.normcase(os.path.normpath(d)))
        subdirs = []
        for e in entries:
            try:
                if e.is_dir():
                    subdirs.append(e.path)
                    continue
            except OSError:
                continue
            if os.path.splitext(e.name)[1].lower() not in AUDIO_EXTS:
                continue
            path = e.path
            stats["scanned"] += 1
            if stats["scanned"] % THROTTLE_EVERY == 0:
                time.sleep(THROTTLE_NAP)
                _write_status(conn, state="scanning", phase="paths", root=root,
                              started_at=t0, **stats)
                if stop_check():
                    break
            try:
                st = e.stat(follow_symlinks=False)  # cached from scandir
            except OSError:
                stats["errors"] += 1
                continue
            seen.add(path)
            prev = known.get(path)
            if prev is not None and prev == (st.st_mtime, st.st_size):
                continue  # unchanged — the incremental fast path
            # record the path now (searchable immediately); tags come in PHASE 2
            title = os.path.splitext(e.name)[0]
            with conn:
                conn.execute(
                    "INSERT INTO tracks (path, title, artist, album, "
                    "  duration_sec, format, size, mtime, indexed_at, "
                    "  missing, tags_read) "
                    "VALUES (?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, 0, 0) "
                    "ON CONFLICT(path) DO UPDATE SET "
                    "  title = excluded.title, artist = NULL, album = NULL, "
                    "  duration_sec = NULL, format = NULL, "
                    "  size = excluded.size, mtime = excluded.mtime, "
                    "  indexed_at = excluded.indexed_at, missing = 0, "
                    "  tags_read = 0",
                    (path, title, st.st_size, st.st_mtime, time.time()))
            stats["added" if prev is None else "updated"] += 1
        # descend last, alphabetically: push reverse-sorted so we pop A->Z
        for sd in sorted(subdirs, key=str.lower, reverse=True):
            stack.append(sd)

    # Flag rows whose files vanished (don't delete — playlists may reference).
    # ONLY flag a file whose folder was actually readable this pass: a
    # flaky/slow NAS that hides a whole subfolder must not mark its tracks
    # missing (that would wipe them from search). Files in folders os.walk
    # couldn't reach are left exactly as they were.
    root_prefix = os.path.join(root, "")
    gone = [p for p in known
            if p not in seen and p.startswith(root_prefix)
            and os.path.normcase(os.path.normpath(os.path.dirname(p)))
            in walked_dirs]
    if gone and not stop_check():
        with conn:
            for p in gone:
                conn.execute("UPDATE tracks SET missing = 1 WHERE path = ?",
                             (p,))
        stats["missing"] = len(gone)
    return stats, seen, walked_dirs


def _backfill_tags(conn: sqlite3.Connection, root: str, t0: float,
                   stats: dict, stop_check) -> int:
    """PHASE 2: read tags for rows still tags_read=0 (newest first, so freshly
    added folders enrich before the long tail). Returns how many were tagged."""
    tagged = 0
    while not stop_check():
        rows = conn.execute(
            "SELECT path FROM tracks WHERE tags_read = 0 AND missing = 0 "
            "ORDER BY indexed_at DESC LIMIT ?", (TAG_BATCH,)).fetchall()
        if not rows:
            break
        for r in rows:
            if stop_check():
                return tagged
            path = r["path"]
            tags = read_tags(path)      # the expensive over-SMB open
            with conn:
                conn.execute(
                    "UPDATE tracks SET title = ?, artist = ?, album = ?, "
                    "  duration_sec = ?, format = ?, tags_read = 1 "
                    "WHERE path = ?",
                    (tags["title"], tags["artist"], tags["album"],
                     tags["duration_sec"], tags["format"], path))
            tagged += 1
            if tagged % THROTTLE_EVERY == 0:
                time.sleep(THROTTLE_NAP)
                left = conn.execute("SELECT COUNT(*) FROM tracks "
                                    "WHERE tags_read = 0 AND missing = 0"
                                    ).fetchone()[0]
                _write_status(conn, state="scanning", phase="tags", root=root,
                              started_at=t0, tags_left=left, tagged=tagged,
                              **stats)
    return tagged


def scan(conn: sqlite3.Connection, root: str,
         stop_check=lambda: False) -> dict:
    """One incremental pass: fast path walk, then tag backfill. Returns
    counters (adds a 'tagged' count on top of the path-phase stats)."""
    t0 = time.time()
    _write_status(conn, state="scanning", phase="paths", root=root,
                  started_at=t0, scanned=0, added=0, updated=0, missing=0,
                  errors=0)
    stats, _seen, _dirs = _walk_paths(conn, root, t0, stop_check)
    log.info("path walk done in %.1fs: %s", time.time() - t0, stats)
    stats["tagged"] = _backfill_tags(conn, root, t0, stats, stop_check)

    left = conn.execute("SELECT COUNT(*) FROM tracks "
                        "WHERE tags_read = 0 AND missing = 0").fetchone()[0]
    _write_status(conn, state="idle", phase="tags", root=root, started_at=t0,
                  finished_at=time.time(), tags_left=left, **stats)
    log.info("scan done in %.1fs: %s (tags left: %d)",
             time.time() - t0, stats, left)
    return stats
