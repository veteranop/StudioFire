"""P2 playlists — CRUD + the dynamic item resolver (PLAN.md §6 Phase 1).

Item types exist in the schema and resolver from day one (retrofitting them
was flagged as painful in review):
  file            — a fixed track path
  folder-newest   — resolves to the newest audio file in a folder at feed
                    time (how syndicated shows slot in, §6 Phase 3)
  folder-rotation — round-robin through a folder with a PERSISTED cursor
                    (§10.5: survives restarts, no unfair skip/replay)

Resolution happens in P2 at feed/precache time. P1 only ever sees concrete
local file paths.
"""

from __future__ import annotations

import logging
import os
import random
import re
import sqlite3
import time

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from pydantic import BaseModel

from . import db as coredb
from .. import pathmap

log = logging.getLogger("core.playlists")

AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".aac", ".wav", ".flac", ".ogg"}
ITEM_TYPES = ("file", "folder-newest", "folder-rotation")

# settings key for the folder StudioFire mirrors playlists into as .lst files
LST_DIR_KEY = "playlist_lst_dir"


# ------------------------------------------------------------------ CRUD

def create_playlist(conn: sqlite3.Connection, name: str) -> int:
    now = time.time()
    with conn:
        cur = conn.execute(
            "INSERT INTO playlists (name, created_at, updated_at) "
            "VALUES (?, ?, ?)", (name, now, now))
    return cur.lastrowid


def parse_lst(data: bytes) -> list[dict]:
    """Parse a ZaraRadio .lst playlist.

    Format: optional first line with the track count, then one track per
    line as '<duration_ms>\\t<path>'. Zara also writes special non-file
    tokens (time events etc.) — anything without an audio extension is
    skipped. Zara is a Windows app, so non-UTF8 files are read as cp1252.
    A negative/garbage duration (seen in the wild: '-2') is treated as
    unknown rather than swallowing the whole line into the path.
    Returns [{"path": ..., "title": ..., "duration": seconds or None}].
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.isdigit():
            continue  # blank line or the count header
        duration = None
        if "\t" in line:
            first, rest = line.split("\t", 1)
            if re.fullmatch(r"-?\d+", first.strip()):
                path = rest.strip()
                ms = int(first)
                duration = ms / 1000.0 if ms > 0 else None
            else:
                path = line
        else:
            path = line
        if os.path.splitext(path)[1].lower() not in AUDIO_EXTS:
            continue
        out.append({"path": path,
                    "title": os.path.splitext(os.path.basename(path))[0],
                    "duration": duration})
    return out


def add_items_bulk(conn: sqlite3.Connection, pid: int,
                   entries: list[dict]) -> int:
    """Append many file items in one transaction (lst import)."""
    with conn:
        pos = conn.execute(
            "SELECT COALESCE(MAX(position) + 1, 0) FROM playlist_items "
            "WHERE playlist_id = ?", (pid,)).fetchone()[0]
        conn.executemany(
            "INSERT INTO playlist_items "
            "  (playlist_id, position, item_type, path, title, duration_sec) "
            "VALUES (?, ?, 'file', ?, ?, ?)",
            [(pid, pos + i, e["path"], e["title"], e.get("duration"))
             for i, e in enumerate(entries)])
        conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?",
                     (time.time(), pid))
    return len(entries)


def rename_playlist(conn: sqlite3.Connection, pid: int, name: str) -> None:
    with conn:
        conn.execute("UPDATE playlists SET name = ?, updated_at = ? "
                     "WHERE id = ?", (name, time.time(), pid))


def delete_playlist(conn: sqlite3.Connection, pid: int) -> None:
    with conn:
        conn.execute("DELETE FROM playlists WHERE id = ?", (pid,))


def duplicate_playlist(conn: sqlite3.Connection, pid: int, name: str) -> int:
    now = time.time()
    with conn:
        cur = conn.execute(
            "INSERT INTO playlists (name, created_at, updated_at) "
            "VALUES (?, ?, ?)", (name, now, now))
        new_id = cur.lastrowid
        conn.execute(
            "INSERT INTO playlist_items "
            "  (playlist_id, position, item_type, path, title, duration_sec) "
            "SELECT ?, position, item_type, path, title, duration_sec "
            "FROM playlist_items WHERE playlist_id = ? ORDER BY position",
            (new_id, pid))
    return new_id


def list_playlists(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT p.*, COUNT(i.id) AS item_count "
        "FROM playlists p LEFT JOIN playlist_items i ON i.playlist_id = p.id "
        "GROUP BY p.id ORDER BY p.name").fetchall()
    return [dict(r) for r in rows]


def get_items(conn: sqlite3.Connection, pid: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM playlist_items WHERE playlist_id = ? "
        "ORDER BY position", (pid,)).fetchall()
    return [dict(r) for r in rows]


def item_durations(conn: sqlite3.Connection, pid: int) -> dict[int, float]:
    """{item_id: seconds} for the playlist's file items whose length is known:
    the item's own stored length first, else the music index's. Folder items
    resolve to a different file each airing, so they have no fixed length."""
    rows = conn.execute(
        "SELECT i.id, COALESCE(i.duration_sec, t.duration_sec) AS dur "
        "FROM playlist_items i LEFT JOIN tracks t ON t.path = i.path "
        "WHERE i.playlist_id = ? AND i.item_type = 'file'", (pid,)).fetchall()
    return {r["id"]: float(r["dur"]) for r in rows if r["dur"]}


def playlist_stats(conn: sqlite3.Connection, pid: int) -> dict:
    """Song count + total run time for a playlist ("how long will this go").
    `unknown` = songs with no known length yet (not counted in total_sec);
    `folders` = folder items (a different file each airing — no fixed length)."""
    items = get_items(conn, pid)
    durs = item_durations(conn, pid)
    songs = sum(1 for i in items if i["item_type"] == "file")
    return {"songs": songs,
            "folders": len(items) - songs,
            "total_sec": round(sum(durs.values()), 1),
            "unknown": songs - len(durs)}


def set_duration_if_unknown(conn: sqlite3.Connection, item_id: int,
                            seconds: float | None) -> None:
    """Remember a file item's real length the first time we learn it (the
    feeder reads it from the cached copy when the song is readied for air)."""
    if not seconds or seconds <= 0:
        return
    with conn:
        conn.execute("UPDATE playlist_items SET duration_sec = ? "
                     "WHERE id = ? AND duration_sec IS NULL "
                     "AND item_type = 'file'",
                     (round(float(seconds), 2), item_id))


# ------------------------------------------------- ZaraRadio .lst export/mirror

def lst_filename(name: str) -> str:
    """A Windows-safe .lst filename for a playlist name."""
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    return (safe or "playlist") + ".lst"


def _expand_folder(conn: sqlite3.Connection, item: dict) -> list[str]:
    """A folder item has no .lst equivalent — expand it to its current files
    from the index (sorted), so the exported .lst is still complete/usable."""
    prefix = (item["path"] or "").rstrip("\\/")
    if not prefix:
        return []
    rows = conn.execute(
        "SELECT path FROM tracks WHERE missing = 0 AND "
        "(path LIKE ? OR path LIKE ?) ORDER BY path",
        (prefix + "\\%", prefix + "/%")).fetchall()
    return [r["path"] for r in rows]


def export_lst_text(conn: sqlite3.Connection, pid: int) -> str:
    """Render a playlist as ZaraRadio .lst text: a track-count header line, then
    one track per line as '<duration_ms>\\t<path>'. Durations come from the
    item itself, else the index (0 when unknown); paths use backslashes, like
    Zara writes them."""
    lines = []
    durs = item_durations(conn, pid)
    for it in get_items(conn, pid):
        if it["item_type"] == "file":
            entries = [(it["path"], durs.get(it["id"]))]
        else:
            entries = [(p, None) for p in _expand_folder(conn, it)]
        for p, dur in entries:
            if dur is None:
                row = conn.execute("SELECT duration_sec FROM tracks "
                                   "WHERE path = ?", (p,)).fetchone()
                dur = row["duration_sec"] if row else None
            ms = int(round(dur * 1000)) if dur else 0
            lines.append(f"{ms}\t{p.replace('/', chr(92))}")
    body = "\r\n".join([str(len(lines))] + lines)
    return body + "\r\n" if lines else "0\r\n"


def remove_lst(lst_dir: str, name: str) -> None:
    lst_dir = alias_path(lst_dir)  # a Z:\ folder still resolves when Z: is gone
    if not lst_dir:
        return
    try:
        os.remove(os.path.join(lst_dir, lst_filename(name)))
    except OSError:
        pass


def write_lst(conn: sqlite3.Connection, lst_dir: str, pid: int,
              old_name: str | None = None) -> str | None:
    """Save a playlist to its .lst file (atomic), Zara-style: the file IS the
    playlist. A playlist saves to its own source_path (the file it was opened
    from / first saved to); one without a source_path gets
    <lst_dir>/<name>.lst and remembers it. A rename renames the file too.
    Best-effort: a save failure must never break the edit itself."""
    row = conn.execute("SELECT name, source_path FROM playlists WHERE id = ?",
                       (pid,)).fetchone()
    if row is None:
        return None
    target = row["source_path"] or ""
    if target and old_name and old_name != row["name"]:
        # renamed -> the file follows (same folder, new name)
        new_target = os.path.join(os.path.dirname(target),
                                  lst_filename(row["name"]))
        if os.path.normcase(new_target) != os.path.normcase(target):
            try:
                os.remove(target)
            except OSError:
                pass
            target = new_target
    if not target:
        if not lst_dir or not os.path.isdir(lst_dir):
            return None
        if old_name and old_name != row["name"]:
            remove_lst(lst_dir, old_name)
        target = os.path.join(lst_dir, lst_filename(row["name"]))
    tmp = target + ".tmp"
    text = export_lst_text(conn, pid)
    try:
        # cp1252 (Windows ANSI) is what Zara reads/writes. A path cp1252 can't
        # hold (e.g. a '√' in a file name) would become '?' and never play
        # again, so such a playlist is saved as UTF-8 with a BOM instead — our
        # parser tries utf-8-sig first, so both round-trip.
        try:
            data = text.encode("cp1252")
        except UnicodeEncodeError:
            data = text.encode("utf-8-sig")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, target)
    except OSError as exc:
        log.warning("could not save playlist %d to .lst: %s", pid, exc)
        return None
    if target != (row["source_path"] or ""):
        with conn:
            conn.execute("UPDATE playlists SET source_path = ? WHERE id = ?",
                         (target, pid))
    return target


# config paths.path_aliases, loaded by register(): {'\\\\SERVER\\share': 'Z:'}
_PATH_ALIASES: dict = {}
# nas_music_root, loaded by register(): lets alias_path() fall back to the NAS
# share root when a stored path's drive letter is gone from this session (the
# dropped-mapped-drive fix — see services/pathmap.py).
_NAS_ROOT: str = ""


def set_path_aliases(aliases: dict | None, nas_root: str | None = None) -> None:
    global _PATH_ALIASES, _NAS_ROOT
    _PATH_ALIASES = dict(aliases or {})
    _NAS_ROOT = nas_root or ""


def alias_path(path: str, aliases: dict | None = None) -> str:
    """Rewrite a path so it resolves on THIS machine even without the drive
    letter it was stored with. Explicit path_aliases are applied first — a
    playlist written elsewhere (e.g. \\\\NAS\\music\\... at the studio)
    maps here (e.g. Z:\\... over a VPN where that name doesn't resolve). Then,
    if the path is on a drive that doesn't exist in this session, it is
    re-pointed at the NAS share root (Z:\\Spots\\... -> \\\\NAS\\music\\
    Spots\\...). Applied at feed time too, not just at import: playlists already
    in the DB keep the paths they were made with."""
    return pathmap.resolve(
        path,
        _PATH_ALIASES if aliases is None else (aliases or {}),
        _NAS_ROOT)


def apply_aliases(entries: list[dict], aliases: dict) -> list[dict]:
    """Rewrite path prefixes ({'\\\\SERVER\\share': 'Z:'}) so a .lst written
    on another machine resolves locally."""
    for e in entries:
        e["path"] = alias_path(e["path"], aliases)
    return entries


def open_lst(conn: sqlite3.Connection, path: str, aliases: dict) -> dict:
    """Open a .lst file as a playlist, Zara-style: the file is the source of
    truth. Already linked -> reuse that playlist (reloading its items from
    the file if something else, e.g. Zara, edited it since our last save).
    New -> create a playlist named after the file and link it."""
    # a .lst path pasted/stored as Z:\... must still open when Z: is gone
    norm = os.path.abspath(alias_path(path))
    if os.path.splitext(norm)[1].lower() != ".lst":
        raise ValueError("not a .lst file")
    if not os.path.isfile(norm):
        raise ValueError("file not found: " + norm)
    row = conn.execute(
        "SELECT id, updated_at FROM playlists "
        "WHERE source_path = ? COLLATE NOCASE", (norm,)).fetchone()
    if row is not None:
        pid = row["id"]
        try:
            mtime = os.path.getmtime(norm)
        except OSError:
            mtime = 0.0
        # our own save lands within ~1s of updated_at; anything clearly newer
        # means an outside edit -> the file wins, reload from it
        if mtime > (row["updated_at"] or 0) + 5:
            with open(norm, "rb") as f:
                entries = apply_aliases(parse_lst(f.read()), aliases)
            with conn:
                conn.execute("DELETE FROM playlist_items "
                             "WHERE playlist_id = ?", (pid,))
            add_items_bulk(conn, pid, entries)
            log.info("playlist %d reloaded from externally edited %s",
                     pid, norm)
        return {"id": pid, "created": False}
    with open(norm, "rb") as f:
        entries = apply_aliases(parse_lst(f.read()), aliases)
    base = os.path.splitext(os.path.basename(norm))[0].strip() or "Playlist"
    name = base
    for i in range(2, 100):  # same name opened from another folder
        try:
            pid = create_playlist(conn, name)
            break
        except sqlite3.IntegrityError:
            name = f"{base} ({i})"
    else:
        raise ValueError("could not find a free playlist name")
    with conn:
        conn.execute("UPDATE playlists SET source_path = ? WHERE id = ?",
                     (norm, pid))
    if entries:
        add_items_bulk(conn, pid, entries)
    return {"id": pid, "created": True}


def sync_all_lst(conn: sqlite3.Connection, lst_dir: str) -> int:
    """Write every playlist to lst_dir (used when the folder is first set, so
    existing playlists get their .lst without needing an edit)."""
    lst_dir = alias_path(lst_dir)  # a Z:\ folder still resolves when Z: is gone
    if not lst_dir or not os.path.isdir(lst_dir):
        return 0
    n = 0
    for row in conn.execute("SELECT id FROM playlists").fetchall():
        if write_lst(conn, lst_dir, row["id"]):
            n += 1
    return n


def add_item(conn: sqlite3.Connection, pid: int, item_type: str,
             path: str, title: str | None = None,
             position: int | None = None,
             duration_sec: float | None = None) -> int:
    if item_type not in ITEM_TYPES:
        raise ValueError(f"bad item_type {item_type!r}")
    with conn:
        if position is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) FROM playlist_items "
                "WHERE playlist_id = ?", (pid,)).fetchone()
            position = row[0]
        else:
            conn.execute(
                "UPDATE playlist_items SET position = position + 1 "
                "WHERE playlist_id = ? AND position >= ?", (pid, position))
        cur = conn.execute(
            "INSERT INTO playlist_items "
            "  (playlist_id, position, item_type, path, title, duration_sec) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pid, position, item_type, path, title,
             duration_sec if item_type == "file" and duration_sec else None))
        conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?",
                     (time.time(), pid))
    return cur.lastrowid


def remove_item(conn: sqlite3.Connection, pid: int, item_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM playlist_items "
                     "WHERE id = ? AND playlist_id = ?", (item_id, pid))
        _renumber(conn, pid)


def reorder_items(conn: sqlite3.Connection, pid: int,
                  ordered_item_ids: list[int]) -> None:
    """Set the playlist order to exactly this id sequence."""
    with conn:
        for pos, item_id in enumerate(ordered_item_ids):
            conn.execute(
                "UPDATE playlist_items SET position = ? "
                "WHERE id = ? AND playlist_id = ?", (pos, item_id, pid))
        _renumber(conn, pid)
        # A manual reorder means the order is the operator's, not ours — the
        # "Shuffled" badge must stop claiming otherwise.
        conn.execute("UPDATE playlists SET updated_at = ?, shuffled_at = NULL "
                     "WHERE id = ?", (time.time(), pid))


def shuffle_items(conn: sqlite3.Connection, pid: int,
                  rng: random.Random | None = None) -> list[int]:
    """Randomly reorder a playlist's items (the operator's Shuffle button) and
    return the new item-id order. Goes through reorder_items, so the DB order
    and the .lst mirror stay exactly in step. A playlist of 0 or 1 items is
    left as-is (there is nothing to shuffle).

    Stamps `shuffled_at` so the UI can SHOW that the order came from a shuffle
    (John, 2026-10-28: "it usually doesn't show me a box ... so I don't think it
    works"). A one-item playlist is stamped too: the operator pressed the
    button and is entitled to see that it registered.
    """
    ids = [i["id"] for i in get_items(conn, pid)]
    if len(ids) > 1:
        (rng or random).shuffle(ids)
        reorder_items(conn, pid, ids)
    with conn:
        conn.execute("UPDATE playlists SET shuffled_at = ? WHERE id = ?",
                     (time.time(), pid))
    return ids


def shuffled_at(conn: sqlite3.Connection, pid: int):
    """When this playlist's order last came from Shuffle, or None."""
    row = conn.execute("SELECT shuffled_at FROM playlists WHERE id = ?",
                       (pid,)).fetchone()
    return row["shuffled_at"] if row else None


def _renumber(conn: sqlite3.Connection, pid: int) -> None:
    rows = conn.execute("SELECT id FROM playlist_items WHERE playlist_id = ? "
                        "ORDER BY position, id", (pid,)).fetchall()
    for pos, row in enumerate(rows):
        conn.execute("UPDATE playlist_items SET position = ? WHERE id = ?",
                     (pos, row["id"]))


# A re-point must never aim into the duplicate-cleanup quarantine: those folders
# are meant to be emptied once the operator is happy, so a playlist pointing into
# one is dead the day it is cleared (2026-11: 56k index rows pointed into
# _Duplicates folders that had already been removed from the NAS).
QUARANTINE_DIR = "\\_duplicates\\"

# Two durations within this many seconds count as the same recording. A re-encode
# keeps a track's length; a different take/song does not. Used only to REFUSE a
# bare-basename guess (see _different_recording), never to pick one.
_DURATION_TOL = 1.5


# A file's NAME without its extension and without a copy marker, so the same
# track matches whether it is stored as Song.mp3, Song.m4a or Song (1).mp3. The
# library has been through several transcodes (mp3 -> m4a/flac) and the duplicate
# cleaner appends " (1)"; a name-only comparison misses both. Same rule as
# duplicates.py::_stem — kept as a copy of the rule, not an import, because that
# module registers HTTP routes and importing it here would drag FastAPI plumbing
# into the playlist layer.
_FILENAME_COPY = re.compile(
    r"\s*(?:\(\s*(?:copy|\d+)\s*\)|\[\s*(?:copy|\d+)\s*\]|[-_]\s*copy|\bcopy)\s*$",
    re.IGNORECASE)


def _file_key(path: str) -> str:
    """The match key for a file: name without extension or copy marker."""
    base = os.path.splitext(os.path.basename(path or ""))[0]
    base = _FILENAME_COPY.sub("", base).strip()
    return re.sub(r"\s+", " ", base).casefold()


def _path_parts(path: str) -> list[str]:
    """A path's components, normcased and separator-agnostic, with the FILENAME
    reduced to _file_key. The library index holds the SAME file as both
    \\\\KDPI-Media\\music\\... and //KDPI-Media/music\\... (two ways the station's
    tools wrote the share), so trailing-segment matching must treat '/' and '\\'
    alike — normcase already lowercases and flips '/'→'\\' on Windows; we flip
    too so it is right off-Windows as well.
    """
    parts = [c for c in os.path.normcase(path).replace("/", "\\").split("\\")
             if c]
    if parts:
        parts[-1] = _file_key(path)
    return parts


def _suffix_depth(a: list[str], b: list[str]) -> int:
    """How many trailing components two component lists share: filename first,
    then parent folder, grandparent, … A depth ≥ 2 means the file name AND at
    least one enclosing folder match — strong evidence it is the same recording
    that moved, even if it was re-encoded to a different size on the way."""
    d = 0
    i, j = len(a) - 1, len(b) - 1
    while i >= 0 and j >= 0 and a[i] == b[j]:
        d += 1
        i -= 1
        j -= 1
    return d


def _different_recording(dead: sqlite3.Row, cand: dict) -> bool:
    """True when the index's own facts PROVE the candidate is a different
    recording from the dead item: size AND duration are both recorded on both
    sides and both differ. This is the guard that stops a bare file-name match
    putting Dread Zeppelin's cover on air where 'Led Zeppelin\\IV\\01 Black
    Dog.mp3' used to be. It is deliberately one-sided — it only ever REFUSES,
    never confirms — and stays silent when either datum is missing (a freshly
    indexed file may not be tag-read yet), so a same-file move or a file in a
    brand-new folder still resolves."""
    ds, cs = dead["size"], cand["size"]
    dd, cd = dead["duration_sec"], cand["duration_sec"]
    return (ds is not None and cs is not None and ds != cs
            and dd is not None and cd is not None
            and abs(dd - cd) > _DURATION_TOL)


def relink_broken(conn: sqlite3.Connection, music_root: str,
                  pid: int | None = None) -> dict:
    """Repoint stale playlist file paths to the real file in the library.

    Three kinds of item, and the INDEX decides which — never a stat per item, so
    a slow NAS link cannot turn this into a walk of the share:

    * ALIVE   — the index holds the path and does not flag it missing. Leave it.
    * DEAD    — the index holds the path and flags it missing, i.e. the indexer
                PROVED the file is gone. The file moved or was renamed, so find
                the present file it became and rewrite the item's path.
    * UNKNOWN — the index has no row at all (a spot/PSA folder outside
                nas_music_root, or something not scanned yet). Leave it alone if
                it is under the root — "not indexed" is not evidence of death —
                otherwise fall back to a unique same-named file, which is how
                playlists imported before the NAS install get repaired.

    The DEAD case is the one that used to be invisible. This function only looked
    at paths OUTSIDE the root, so an item whose file moved WITHIN the library (a
    folder renamed on the NAS) was counted "in_place" and skipped — the operator
    pressed "Fix broken file paths", got "0 track(s) re-pointed", and 66% of the
    on-air playlist stayed dead while the files sat one folder away.

    Finding the file it BECAME — the matching ladder, first stage with a single
    winner wins (all against the index, no walk of the share):

      1. Trailing-segment match. Among present files sharing the basename, score
         by how many trailing path components also match. A unique winner at
         depth ≥ 2 (name + ≥1 folder) IS the moved file — this catches a folder
         un-nested (``New JB Music\\Neil Young\\x`` → ``Neil Young\\x``) and a
         re-encode that kept its album folder, where the size differs but the
         folder path corroborates.
      2. Size tie-break. If several tie at the deepest match, prefer the one
         whose recorded size equals the dead row's (the byte-identical file that
         moved). No unique size winner → ambiguous, left alone.
      0. The NAME compares extension- and copy-marker-insensitively, so a file
         that was transcoded (Song.mp3 -> Song.m4a) or copied by the duplicate
         cleaner (Song (1).mp3) still matches its own recording.
      3. Bare-basename fallback. A single same-named survivor at depth 1 (only
         the file name matches — a folder renamed AND flattened, e.g. ``Charlie
         Parker … Birdsong\\18 Big Foot`` → ``charlie parker new\\18 Big Foot``).
         Here the folder gives no corroboration, so a size+duration contradiction
         (``_different_recording``) REFUSES the guess rather than risk the wrong
         recording on air.

    Safety rails: ambiguity is never guessed; the quarantine folder is never a
    target; an item is never re-pointed at its own path. When the share is
    reachable (probed ONCE) the chosen target is confirmed on disk before the
    rewrite, and a DEAD item whose own file is back on disk is healed in place
    instead of hijacked to a same-named copy. On a host where the share does not
    resolve (``verified_on_disk`` is False in the result) the re-points are still
    computed from the index so the repair is reproducible, just without the probe.
    """
    import collections
    root_norm = os.path.normcase(os.path.normpath(music_root)) + os.sep
    by_base: dict[str, list] = collections.defaultdict(list)
    known: dict[str, int] = {}          # normalised path -> index `missing` flag
    dead_meta: dict[str, sqlite3.Row] = {}   # normalised path -> the dead row
    for row in conn.execute(
            "SELECT path, missing, size, duration_sec FROM tracks"):
        key = os.path.normcase(row["path"])
        known[key] = row["missing"] or 0
        if row["missing"]:
            dead_meta[key] = row
        else:
            by_base[_file_key(row["path"])].append(
                {"path": row["path"], "parts": _path_parts(row["path"]),
                 "size": row["size"], "duration_sec": row["duration_sec"]})

    # Probe the share ONCE. A hard isfile() gate on every re-point would make the
    # function do nothing on a host that can't see the share (Mark's workstation
    # can't reach \\KDPI-Media; the on-air box can) and silently stop the repair
    # from being reproducible — so when the root isn't reachable we skip the
    # on-disk checks and say so, rather than re-point nothing.
    can_verify = os.path.isdir(music_root)

    pids = ([pid] if pid is not None
            else [r["id"] for r in conn.execute("SELECT id FROM playlists")])
    stats = {"checked": 0, "in_place": 0, "relinked": 0, "ambiguous": 0,
             "unmatched": 0, "dead": 0, "conflict": 0, "self_healed": 0,
             "unverified": 0, "by_suffix": 0, "by_size": 0, "by_basename": 0,
             "verified_on_disk": can_verify, "unmatched_examples": []}

    def _unmatched(it):
        stats["unmatched"] += 1
        if len(stats["unmatched_examples"]) < 25:
            stats["unmatched_examples"].append(os.path.basename(it["path"]))

    with conn:
        for p in pids:
            items = conn.execute(
                "SELECT id, path FROM playlist_items "
                "WHERE playlist_id = ? AND item_type = 'file'", (p,)).fetchall()
            for it in items:
                stats["checked"] += 1
                key = os.path.normcase(it["path"])
                missing = known.get(key)
                if missing == 0:
                    stats["in_place"] += 1            # the index says it is there
                    continue
                if missing == 1:
                    stats["dead"] += 1                # the index PROVED it is gone
                elif key.startswith(root_norm):
                    stats["in_place"] += 1            # not indexed != dead
                    continue

                cands = [c for c in
                         by_base.get(_file_key(it["path"])) or ()
                         if os.path.normcase(c["path"]) != key
                         and QUARANTINE_DIR not in os.path.normcase(c["path"])]
                if not cands:
                    _unmatched(it)
                    continue

                # Self-heal: a DEAD item whose own file is back on disk (a
                # restore) must not be hijacked to a same-named copy. Checked
                # only when there IS a candidate to hijack with, so the stat
                # stays bounded (never a walk of every dead item).
                if missing == 1 and can_verify and os.path.isfile(it["path"]):
                    conn.execute("UPDATE tracks SET missing = 0 WHERE path = ?",
                                 (it["path"],))
                    stats["self_healed"] += 1
                    stats["in_place"] += 1
                    continue

                dead = dead_meta.get(key)
                it_parts = _path_parts(it["path"])
                scored = [(_suffix_depth(it_parts, c["parts"]), c)
                          for c in cands]
                best = max(d for d, _ in scored)
                winners = [c for d, c in scored if d == best]

                winner = stage = None
                if len(winners) == 1:
                    cand = winners[0]
                    if best >= 2:
                        winner, stage = cand, "by_suffix"
                    elif dead is not None and _different_recording(dead, cand):
                        stats["conflict"] += 1       # same name, proven not it
                        continue
                    else:
                        winner, stage = cand, "by_basename"
                else:
                    if dead is not None and dead["size"] is not None:
                        tied = [c for c in winners if c["size"] == dead["size"]]
                        if len(tied) == 1:
                            winner, stage = tied[0], "by_size"
                    if winner is None:
                        stats["ambiguous"] += 1       # same name in 2+ folders
                        continue

                # Confirm the chosen target exists before rewriting, so a stale
                # index row can't replace a dead entry with another dead one.
                # One stat per re-point, never per item.
                if can_verify and not os.path.isfile(winner["path"]):
                    stats["unverified"] += 1
                    _unmatched(it)
                    continue

                conn.execute("UPDATE playlist_items SET path = ? WHERE id = ?",
                             (winner["path"], it["id"]))
                stats["relinked"] += 1
                stats[stage] += 1
    return stats


def probe_duration(conn: sqlite3.Connection, path: str) -> float | None:
    """A file's length in seconds: from the music index if it's there, else
    read from the file's own header (one small read — fine for a single add,
    never used in bulk). None if unknown/unreadable. Never raises."""
    row = conn.execute("SELECT duration_sec FROM tracks WHERE path = ?",
                       (path,)).fetchone()
    if row and row["duration_sec"]:
        return float(row["duration_sec"])
    try:
        import mutagen
        m = mutagen.File(path)
        length = getattr(getattr(m, "info", None), "length", None)
        return round(float(length), 2) if length else None
    except Exception:  # noqa: BLE001 — mutagen raises wildly varied errors
        return None


# -------------------------------------------------------------- resolver

def _audio_files(folder: str) -> list[str]:
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(folder, n)
        if os.path.splitext(n)[1].lower() in AUDIO_EXTS and os.path.isfile(p):
            out.append(p)
    return out


def resolve_item(conn: sqlite3.Connection, item: dict) -> str | None:
    """Turn a playlist item into a concrete file path at feed time.
    Returns None when nothing is available (skip + alert, never failover —
    §10.5)."""
    kind, path = item["item_type"], alias_path(item["path"])
    if kind == "file":
        return path if os.path.isfile(path) else None
    files = _audio_files(path)
    if not files:
        return None
    if kind == "folder-newest":
        return max(files, key=lambda p: os.path.getmtime(p))
    if kind == "folder-random":
        import random
        return random.choice(files)
    if kind == "folder-rotation":
        files.sort(key=lambda p: os.path.basename(p).lower())
        row = conn.execute(
            "SELECT next_index FROM rotation_state WHERE folder_path = ?",
            (path,)).fetchone()
        idx = (row["next_index"] if row else 0) % len(files)
        with conn:
            conn.execute(
                "INSERT INTO rotation_state (folder_path, next_index, "
                "updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(folder_path) DO UPDATE SET "
                "next_index = excluded.next_index, "
                "updated_at = excluded.updated_at",
                (path, idx + 1, time.time()))
        return files[idx]
    return None


# ------------------------------------------------------------------- API

class PlaylistIn(BaseModel):
    name: str


class ItemIn(BaseModel):
    item_type: str
    path: str
    title: str | None = None
    position: int | None = None
    duration_sec: float | None = None


class OrderIn(BaseModel):
    item_ids: list[int]


class OpenIn(BaseModel):
    path: str


def register(app: FastAPI) -> None:
    get_conn = app.state.get_conn
    api_user = app.state.api_user
    set_path_aliases(app.state.cfg.get("path_aliases"),
                     app.state.cfg.get("nas_music_root"))

    def _playlist_or_404(conn, pid: int):
        row = conn.execute("SELECT * FROM playlists WHERE id = ?",
                           (pid,)).fetchone()
        if row is None:
            raise HTTPException(404, "playlist not found")
        return row

    def _lst_dir(conn) -> str:
        """The playlists folder: where NEW playlists are saved as .lst.
        The operator-set folder wins; the fallback (<data>/playlists) exists
        so a playlist is ALWAYS a real file, even before any setup."""
        # resolve so a Z:\ playlists folder still points at the NAS share root
        # (\\NAS\music) when this session has no Z: mapping
        d = alias_path(coredb.get_setting(conn, LST_DIR_KEY) or "")
        if d and os.path.isdir(d):
            return d
        d = os.path.join(app.state.cfg["data_dir"], "playlists")
        os.makedirs(d, exist_ok=True)
        return d

    def _sync(conn, pid, old_name=None):
        """Save a playlist to its .lst file after a change. Best-effort — a
        save failure must never fail the edit the operator just made."""
        try:
            write_lst(conn, _lst_dir(conn), pid, old_name)
        except Exception:  # noqa: BLE001 — saving the file is never critical
            log.exception("playlist .lst save failed (pid %s)", pid)

    @app.get("/api/playlists")
    def api_list(conn=Depends(get_conn), _=Depends(api_user)):
        return list_playlists(conn)

    @app.post("/api/playlists", status_code=201)
    def api_create(body: PlaylistIn, conn=Depends(get_conn),
                   _=Depends(api_user)):
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name required")
        try:
            pid = create_playlist(conn, name)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "a playlist with that name exists")
        _sync(conn, pid)
        return {"id": pid, "name": name}

    @app.post("/api/playlists/open", status_code=200)
    def api_open(body: OpenIn, conn=Depends(get_conn), _=Depends(api_user)):
        """Open a .lst file as a playlist (creating/linking as needed) —
        the file-explorer 'Open a playlist' flow."""
        try:
            return open_lst(conn, body.path,
                            app.state.cfg.get("path_aliases") or {})
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except OSError as exc:
            raise HTTPException(400, f"cannot read that file: {exc}")

    @app.post("/api/playlists/import_lst", status_code=201)
    def api_import_lst(file: UploadFile, name: str = Form(...),
                       conn=Depends(get_conn), _=Depends(api_user)):
        """Upload a .lst from the operator's own computer (the file-explorer
        Open flow covers files the SERVER can see; this covers the rest)."""
        entries = apply_aliases(parse_lst(file.file.read()),
                                app.state.cfg.get("path_aliases") or {})
        if not entries:
            raise HTTPException(400, "no playable tracks found in that file")
        try:
            pid = create_playlist(conn, name.strip() or "Imported playlist")
        except sqlite3.IntegrityError:
            raise HTTPException(409, "a playlist with that name exists")
        n = add_items_bulk(conn, pid, entries)
        _sync(conn, pid)
        return {"id": pid, "name": name.strip(), "imported": n}

    # NOTE: fixed paths must be declared BEFORE /{pid} — FastAPI matches in
    # declaration order, so /api/playlists/lst_dir after /{pid} would try to
    # parse "lst_dir" as a playlist id (422).
    @app.get("/api/playlists/lst_dir")
    def api_lst_dir_get(conn=Depends(get_conn), _=Depends(api_user)):
        path = coredb.get_setting(conn, LST_DIR_KEY) or ""
        # report the truth against the resolved location, not the raw Z:\ value
        return {"path": path,
                "exists": bool(path) and os.path.isdir(alias_path(path)),
                # where new playlists actually land / where Open starts
                "effective": _lst_dir(conn)}

    @app.get("/api/playlists/{pid}")
    def api_get(pid: int, conn=Depends(get_conn), _=Depends(api_user)):
        row = _playlist_or_404(conn, pid)
        return {"id": row["id"], "name": row["name"],
                "items": get_items(conn, pid)}

    @app.post("/api/playlists/{pid}/rename")
    def api_rename(pid: int, body: PlaylistIn, conn=Depends(get_conn),
                   _=Depends(api_user)):
        old = _playlist_or_404(conn, pid)["name"]
        try:
            rename_playlist(conn, pid, body.name.strip())
        except sqlite3.IntegrityError:
            raise HTTPException(409, "a playlist with that name exists")
        _sync(conn, pid, old_name=old)  # writes new name, deletes old .lst
        return {"ok": True}

    @app.post("/api/playlists/{pid}/duplicate", status_code=201)
    def api_duplicate(pid: int, body: PlaylistIn, conn=Depends(get_conn),
                      _=Depends(api_user)):
        _playlist_or_404(conn, pid)
        try:
            new_id = duplicate_playlist(conn, pid, body.name.strip())
        except sqlite3.IntegrityError:
            raise HTTPException(409, "a playlist with that name exists")
        _sync(conn, new_id)
        return {"id": new_id}

    @app.delete("/api/playlists/{pid}")
    def api_delete(pid: int, conn=Depends(get_conn), _=Depends(api_user)):
        row = _playlist_or_404(conn, pid)
        delete_playlist(conn, pid)
        # Zara-style: deleting the playlist deletes its .lst file
        src = row["source_path"] if "source_path" in row.keys() else None
        if src:
            try:
                os.remove(alias_path(src))  # delete the real file even if Z: is gone
            except OSError:
                pass
        else:
            remove_lst(coredb.get_setting(conn, LST_DIR_KEY) or "", row["name"])
        return {"ok": True}

    @app.post("/api/playlists/{pid}/items", status_code=201)
    def api_add_item(pid: int, body: ItemIn, conn=Depends(get_conn),
                     _=Depends(api_user)):
        _playlist_or_404(conn, pid)
        if body.item_type not in ITEM_TYPES:
            raise HTTPException(400, f"item_type must be one of {ITEM_TYPES}")
        dur = body.duration_sec
        if body.item_type == "file" and not dur:
            dur = probe_duration(conn, body.path)
        item_id = add_item(conn, pid, body.item_type, body.path,
                           body.title, body.position, duration_sec=dur)
        _sync(conn, pid)
        return {"id": item_id,
                "duration_sec": dur if body.item_type == "file" else None}

    @app.get("/api/playlists/{pid}/stats")
    def api_stats(pid: int, conn=Depends(get_conn), _=Depends(api_user)):
        """Song count + total run time ("how long will this playlist go")."""
        _playlist_or_404(conn, pid)
        return playlist_stats(conn, pid)

    @app.delete("/api/playlists/{pid}/items/{item_id}")
    def api_remove_item(pid: int, item_id: int, conn=Depends(get_conn),
                        _=Depends(api_user)):
        _playlist_or_404(conn, pid)
        remove_item(conn, pid, item_id)
        _sync(conn, pid)
        return {"ok": True}

    @app.post("/api/playlists/{pid}/order")
    def api_reorder(pid: int, body: OrderIn, conn=Depends(get_conn),
                    _=Depends(api_user)):
        _playlist_or_404(conn, pid)
        existing = {i["id"] for i in get_items(conn, pid)}
        if set(body.item_ids) != existing:
            raise HTTPException(409, "order list out of sync — reload")
        reorder_items(conn, pid, body.item_ids)
        _sync(conn, pid)
        return {"ok": True}

    @app.post("/api/playlists/{pid}/shuffle")
    def api_shuffle(pid: int, conn=Depends(get_conn), _=Depends(api_user)):
        """Shuffle the playlist into a random order (the Shuffle button).
        Returns the new item-id order so the page can redraw without a reload."""
        _playlist_or_404(conn, pid)
        ids = shuffle_items(conn, pid)
        _sync(conn, pid)          # the .lst file follows the new order
        return {"ok": True, "item_ids": ids,
                "shuffled_at": shuffled_at(conn, pid)}

    def _music_root() -> str:
        root = (app.state.cfg.get("nas_music_root") or "").strip()
        if not root:
            raise HTTPException(400, "music folder (nas_music_root) not set")
        return root

    @app.post("/api/playlists/relink")
    def api_relink_all(conn=Depends(get_conn), _=Depends(api_user)):
        """Repoint stale paths in ALL playlists to the real file in the index."""
        result = relink_broken(conn, _music_root())
        sync_all_lst(conn, coredb.get_setting(conn, LST_DIR_KEY) or "")
        return result

    @app.post("/api/playlists/{pid}/relink")
    def api_relink_one(pid: int, conn=Depends(get_conn), _=Depends(api_user)):
        _playlist_or_404(conn, pid)
        result = relink_broken(conn, _music_root(), pid)
        _sync(conn, pid)
        return result

    @app.post("/api/playlists/lst_dir")
    def api_lst_dir_set(body: PlaylistIn, conn=Depends(get_conn),
                        _=Depends(api_user)):
        """Set the folder playlists are mirrored to as .lst, and immediately
        write every existing playlist there so they're all covered."""
        path = (body.name or "").strip()
        # validate against the resolved location so an operator may keep a Z:\
        # folder that maps to the NAS; store the raw value (resolve, don't rewrite)
        if path and not os.path.isdir(alias_path(path)):
            raise HTTPException(400, "that folder does not exist")
        coredb.set_setting(conn, LST_DIR_KEY, path)
        n = sync_all_lst(conn, path) if path else 0
        return {"ok": True, "path": path, "synced": n}

    @app.post("/api/playlists/sync_lst")
    def api_sync_lst(conn=Depends(get_conn), _=Depends(api_user)):
        """Re-mirror every playlist to the configured .lst folder now."""
        lst_dir = coredb.get_setting(conn, LST_DIR_KEY) or ""
        if not lst_dir:
            raise HTTPException(400, "set the playlist .lst folder first")
        return {"synced": sync_all_lst(conn, lst_dir), "folder": lst_dir}
