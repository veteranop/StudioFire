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

import os
import sqlite3
import time

from fastapi import Depends, FastAPI, Form, HTTPException, UploadFile
from pydantic import BaseModel

AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".aac", ".wav", ".flac", ".ogg"}
ITEM_TYPES = ("file", "folder-newest", "folder-rotation")


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
    Returns [{"path": ..., "title": ...}].
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
        if "\t" in line:
            first, rest = line.split("\t", 1)
            path = rest.strip() if first.strip().isdigit() else line
        else:
            path = line
        if os.path.splitext(path)[1].lower() not in AUDIO_EXTS:
            continue
        out.append({"path": path,
                    "title": os.path.splitext(os.path.basename(path))[0]})
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
            "  (playlist_id, position, item_type, path, title) "
            "VALUES (?, ?, 'file', ?, ?)",
            [(pid, pos + i, e["path"], e["title"])
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
            "  (playlist_id, position, item_type, path, title) "
            "SELECT ?, position, item_type, path, title "
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


def add_item(conn: sqlite3.Connection, pid: int, item_type: str,
             path: str, title: str | None = None,
             position: int | None = None) -> int:
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
            "  (playlist_id, position, item_type, path, title) "
            "VALUES (?, ?, ?, ?, ?)", (pid, position, item_type, path, title))
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
        conn.execute("UPDATE playlists SET updated_at = ? WHERE id = ?",
                     (time.time(), pid))


def _renumber(conn: sqlite3.Connection, pid: int) -> None:
    rows = conn.execute("SELECT id FROM playlist_items WHERE playlist_id = ? "
                        "ORDER BY position, id", (pid,)).fetchall()
    for pos, row in enumerate(rows):
        conn.execute("UPDATE playlist_items SET position = ? WHERE id = ?",
                     (pos, row["id"]))


def relink_broken(conn: sqlite3.Connection, music_root: str,
                  pid: int | None = None) -> dict:
    """Repoint stale playlist file paths to the real file in the library.

    Many playlists (imported before the Synology install) point at old
    locations OUTSIDE the current music root. The files just moved — same
    name, now under music_root. For each file item whose path is not under
    music_root, look up the same-named file in the index (present only) and,
    on a UNIQUE match, rewrite the item's path. Pure DB work — no NAS access,
    so it's fast even over a slow link. Items already under the root are left
    alone (they're correct, or just not scanned yet).
    """
    import collections
    root_norm = os.path.normcase(os.path.normpath(music_root)) + os.sep
    by_base: dict[str, set] = collections.defaultdict(set)
    for row in conn.execute("SELECT path FROM tracks WHERE missing = 0"):
        by_base[os.path.normcase(os.path.basename(row["path"]))].add(row["path"])

    pids = ([pid] if pid is not None
            else [r["id"] for r in conn.execute("SELECT id FROM playlists")])
    stats = {"checked": 0, "in_place": 0, "relinked": 0,
             "ambiguous": 0, "unmatched": 0, "unmatched_examples": []}
    with conn:
        for p in pids:
            items = conn.execute(
                "SELECT id, path FROM playlist_items "
                "WHERE playlist_id = ? AND item_type = 'file'", (p,)).fetchall()
            for it in items:
                stats["checked"] += 1
                if os.path.normcase(os.path.normpath(it["path"])).startswith(
                        root_norm):
                    stats["in_place"] += 1            # already under the root
                    continue
                matches = by_base.get(os.path.normcase(
                    os.path.basename(it["path"])))
                if not matches:
                    stats["unmatched"] += 1
                    if len(stats["unmatched_examples"]) < 25:
                        stats["unmatched_examples"].append(
                            os.path.basename(it["path"]))
                    continue
                if len(matches) > 1:
                    stats["ambiguous"] += 1           # same name in 2+ folders
                    continue
                conn.execute("UPDATE playlist_items SET path = ? WHERE id = ?",
                             (next(iter(matches)), it["id"]))
                stats["relinked"] += 1
    return stats


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
    kind, path = item["item_type"], item["path"]
    if kind == "file":
        return path if os.path.isfile(path) else None
    files = _audio_files(path)
    if not files:
        return None
    if kind == "folder-newest":
        return max(files, key=lambda p: os.path.getmtime(p))
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


class OrderIn(BaseModel):
    item_ids: list[int]


def register(app: FastAPI) -> None:
    get_conn = app.state.get_conn
    api_user = app.state.api_user

    def _playlist_or_404(conn, pid: int):
        row = conn.execute("SELECT * FROM playlists WHERE id = ?",
                           (pid,)).fetchone()
        if row is None:
            raise HTTPException(404, "playlist not found")
        return row

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
        return {"id": pid, "name": name}

    @app.post("/api/playlists/import_lst", status_code=201)
    def api_import_lst(file: UploadFile, name: str = Form(...),
                       conn=Depends(get_conn), _=Depends(api_user)):
        """Import a ZaraRadio .lst playlist as a new playlist."""
        entries = parse_lst(file.file.read())
        if not entries:
            raise HTTPException(400, "no playable tracks found in that file")
        aliases = app.state.cfg.get("path_aliases") or {}
        for e in entries:
            for prefix, repl in aliases.items():
                if e["path"].lower().startswith(prefix.lower()):
                    e["path"] = repl + e["path"][len(prefix):]
                    break
        try:
            pid = create_playlist(conn, name.strip() or "Imported playlist")
        except sqlite3.IntegrityError:
            raise HTTPException(409, "a playlist with that name exists")
        n = add_items_bulk(conn, pid, entries)
        return {"id": pid, "name": name.strip(), "imported": n}

    @app.get("/api/playlists/{pid}")
    def api_get(pid: int, conn=Depends(get_conn), _=Depends(api_user)):
        row = _playlist_or_404(conn, pid)
        return {"id": row["id"], "name": row["name"],
                "items": get_items(conn, pid)}

    @app.post("/api/playlists/{pid}/rename")
    def api_rename(pid: int, body: PlaylistIn, conn=Depends(get_conn),
                   _=Depends(api_user)):
        _playlist_or_404(conn, pid)
        try:
            rename_playlist(conn, pid, body.name.strip())
        except sqlite3.IntegrityError:
            raise HTTPException(409, "a playlist with that name exists")
        return {"ok": True}

    @app.post("/api/playlists/{pid}/duplicate", status_code=201)
    def api_duplicate(pid: int, body: PlaylistIn, conn=Depends(get_conn),
                      _=Depends(api_user)):
        _playlist_or_404(conn, pid)
        try:
            new_id = duplicate_playlist(conn, pid, body.name.strip())
        except sqlite3.IntegrityError:
            raise HTTPException(409, "a playlist with that name exists")
        return {"id": new_id}

    @app.delete("/api/playlists/{pid}")
    def api_delete(pid: int, conn=Depends(get_conn), _=Depends(api_user)):
        _playlist_or_404(conn, pid)
        delete_playlist(conn, pid)
        return {"ok": True}

    @app.post("/api/playlists/{pid}/items", status_code=201)
    def api_add_item(pid: int, body: ItemIn, conn=Depends(get_conn),
                     _=Depends(api_user)):
        _playlist_or_404(conn, pid)
        if body.item_type not in ITEM_TYPES:
            raise HTTPException(400, f"item_type must be one of {ITEM_TYPES}")
        item_id = add_item(conn, pid, body.item_type, body.path,
                           body.title, body.position)
        return {"id": item_id}

    @app.delete("/api/playlists/{pid}/items/{item_id}")
    def api_remove_item(pid: int, item_id: int, conn=Depends(get_conn),
                        _=Depends(api_user)):
        _playlist_or_404(conn, pid)
        remove_item(conn, pid, item_id)
        return {"ok": True}

    @app.post("/api/playlists/{pid}/order")
    def api_reorder(pid: int, body: OrderIn, conn=Depends(get_conn),
                    _=Depends(api_user)):
        _playlist_or_404(conn, pid)
        existing = {i["id"] for i in get_items(conn, pid)}
        if set(body.item_ids) != existing:
            raise HTTPException(409, "order list out of sync — reload")
        reorder_items(conn, pid, body.item_ids)
        return {"ok": True}

    def _music_root() -> str:
        root = (app.state.cfg.get("nas_music_root") or "").strip()
        if not root:
            raise HTTPException(400, "music folder (nas_music_root) not set")
        return root

    @app.post("/api/playlists/relink")
    def api_relink_all(conn=Depends(get_conn), _=Depends(api_user)):
        """Repoint stale paths in ALL playlists to the real file in the index."""
        return relink_broken(conn, _music_root())

    @app.post("/api/playlists/{pid}/relink")
    def api_relink_one(pid: int, conn=Depends(get_conn), _=Depends(api_user)):
        _playlist_or_404(conn, pid)
        return relink_broken(conn, _music_root(), pid)
