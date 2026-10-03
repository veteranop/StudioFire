"""P2 library duplicate finder + safe cleanup (ticket 703, John's request).

Two halves, both deliberately conservative:

  find_duplicates()  READ-ONLY. Groups the music index by a filename key — the
      file's name with any copy marker removed (``Song (1)`` / ``Song - Copy``
      -> ``Song``), case-folded and whitespace-collapsed — and returns every
      group that still holds 2+ present files. For each file it reports the
      size, length, who references it, and whether it is protected, and it
      suggests ONE to keep (a protected file first, then the one with tags,
      then the largest). Nothing on disk is touched.

  quarantine()       The operator's cleanup. Moves the files they did NOT keep
      into ``<music_root>/_Duplicates/<date>/`` mirroring the folder layout, and
      appends a ``manifest.json`` there. It MOVES — it never deletes — so the
      whole thing is reversible by hand from the manifest.

Why move, not delete: John said "some tracks are intentionally different
versions of the same song and 1-2 copies should be kept". A mandatory preview
step plus a reversible move is the only safe way to touch a live library.

A file is PROTECTED (never moved) when it is referenced by a playlist item, a
scheduled show/`.lst`, or a spot; or when it lives inside a station folder
(the five dir_* settings) or a spot's folder. Protected files are still shown
in the preview — the operator just cannot sweep them away by accident.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import shutil
import sqlite3
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import db as coredb
from . import spots

log = logging.getLogger("core.duplicates")

#  "Song (1)" / "Song (2)" / "Song [Copy]" / "Song - Copy" / "Song copy" -> "Song"
_COPY_SUFFIX = re.compile(
    r"\s*(?:\(\s*(?:copy|\d+)\s*\)|\[\s*(?:copy|\d+)\s*\]|[-_]\s*copy|\bcopy)\s*$",
    re.IGNORECASE)


def _norm(s: str | None) -> str:
    """Case-fold + collapse whitespace, for a stable grouping key."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s).strip()).casefold()


def _stem(path: str) -> str:
    """The grouping key for a file: its name without the extension, with any
    trailing copy marker stripped."""
    base = os.path.splitext(os.path.basename(path or ""))[0]
    return _norm(_COPY_SUFFIX.sub("", base).strip())


def _np(path: str) -> str:
    return os.path.normcase(os.path.normpath(path or ""))


# ----------------------------------------------------------------- protection

def _protected(conn: sqlite3.Connection) -> tuple[set, list[str]]:
    """(exact files, protected folder prefixes) that a cleanup must never move.

    Exact: every playlist item, scheduled file/.lst source, and spot file.
    Folders: the five station folders (dir_*) and any spot's chosen folder —
    files inside a curated folder are off-limits even if they look duplicate.
    """
    exact: set = set()
    for (p,) in conn.execute(
            "SELECT path FROM playlist_items WHERE item_type = 'file'"):
        if p:
            exact.add(_np(p))
    for (p,) in conn.execute(
            "SELECT source_path FROM playlist_schedule "
            "WHERE source_kind IN ('file', 'lst') AND source_path IS NOT NULL"):
        if p:
            exact.add(_np(p))
    for (p,) in conn.execute(
            "SELECT file_path FROM spot_rules WHERE file_path IS NOT NULL"):
        if p:
            exact.add(_np(p))

    prefixes: list[str] = []
    for key, _, _ in spots.FOLDER_CATEGORIES:
        v = (coredb.get_setting(conn, key) or "").strip()
        if v:
            prefixes.append(_np(v).rstrip("\\/") + os.sep)
    for (p,) in conn.execute(
            "SELECT folder_path FROM spot_rules WHERE folder_path IS NOT NULL"):
        if p and p.strip():
            prefixes.append(_np(p).rstrip("\\/") + os.sep)
    return exact, prefixes


def _is_protected(path: str, exact: set, prefixes: list[str]) -> bool:
    np = _np(path)
    return np in exact or any(np.startswith(p) for p in prefixes)


# --------------------------------------------------------------- the finder

def _used_by(conn: sqlite3.Connection) -> dict:
    """{normcase(path): [playlist names]} for the 'used by' badge."""
    out: dict = {}
    for row in conn.execute(
            "SELECT i.path AS path, p.name AS name FROM playlist_items i "
            "JOIN playlists p ON p.id = i.playlist_id "
            "WHERE i.item_type = 'file'"):
        if row["path"]:
            out.setdefault(_np(row["path"]), []).append(row["name"])
    return out


def find_duplicates(conn: sqlite3.Connection, music_root: str = "") -> dict:
    """READ-ONLY. Every group of 2+ present files sharing a filename key.
    Each file carries size/length/used_by/protected; exactly one per group is
    flagged suggested_keep (protected first, then tagged, then largest)."""
    exact, prefixes = _protected(conn)
    used = _used_by(conn)

    groups: dict[str, list] = {}
    for r in conn.execute(
            "SELECT path, title, artist, album, duration_sec, size, format "
            "FROM tracks WHERE missing = 0"):
        key = _stem(r["path"])
        if key:
            groups.setdefault(key, []).append(r)

    out = []
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        files = []
        for r in rows:
            files.append({
                "path": r["path"],
                "name": os.path.basename(r["path"]),
                "folder": os.path.dirname(r["path"]),
                "artist": r["artist"],
                "title": r["title"],
                "album": r["album"],
                "duration_sec": r["duration_sec"],
                "size": r["size"],
                "format": r["format"],
                "used_by": sorted(set(used.get(_np(r["path"]), []))),
                "protected": _is_protected(r["path"], exact, prefixes),
            })
        # keep: a referenced/protected file first (those must never be swept),
        # then a tagged file, then the largest (usually the better encoding),
        # then a stable path order
        files.sort(key=lambda f: (0 if (f["protected"] or f["used_by"]) else 1,
                                  not bool(f["artist"]),
                                  -(f["size"] or 0),
                                  f["path"].casefold()))
        files[0]["suggested_keep"] = True
        for f in files[1:]:
            f["suggested_keep"] = False
        out.append({"key": key,
                    "title": files[0]["title"] or key,
                    "artist": files[0]["artist"] or "",
                    "count": len(files),
                    "files": files})
    out.sort(key=lambda g: (g["artist"].casefold(), g["title"].casefold()))
    return {"music_root": music_root,
            "groups": out,
            "group_count": len(out),
            "file_count": sum(g["count"] for g in out)}


# ------------------------------------------------------------- the cleanup

def _rel_under(path: str, root: str) -> str | None:
    """The path relative to the music root, or None if it is outside it."""
    if not root:
        return None
    nroot = _np(root).rstrip("\\/")
    npath = _np(path)
    if npath == nroot:
        return os.path.basename(path)
    if not npath.startswith(nroot + os.sep):
        return None
    return os.path.normpath(path)[len(os.path.normpath(root).rstrip("\\/")) + 1:]


def _unique(dest: str) -> str:
    """Never clobber an existing quarantined file — add ' (2)', ' (3)', ..."""
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(dest)
    for n in range(2, 1000):
        cand = f"{stem} ({n}){ext}"
        if not os.path.exists(cand):
            return cand
    return dest


def quarantine(conn: sqlite3.Connection, remove_paths: list[str],
               music_root: str) -> dict:
    """Move the given files into <music_root>/_Duplicates/<date>/ (layout
    mirrored) and write manifest.json. Protected files and protected folders
    are skipped, never moved. Returns {moved, skipped, errors, dest}."""
    if not music_root:
        raise HTTPException(400, "music folder (nas_music_root) is not set")
    exact, prefixes = _protected(conn)
    dest_root = os.path.join(music_root, "_Duplicates",
                             _dt.date.today().isoformat())
    moved, skipped, errors, manifest = [], [], [], []

    for p in remove_paths:
        if not p or not str(p).strip():
            continue
        p = str(p)
        if _is_protected(p, exact, prefixes):
            reason = ("used by a playlist/show/spot"
                      if _np(p) in exact else "inside a protected folder")
            skipped.append({"path": p, "reason": reason})
            continue
        if not os.path.isfile(p):
            errors.append({"path": p, "reason": "file not found"})
            continue
        rel = _rel_under(p, music_root)
        dest = (os.path.join(dest_root, rel) if rel is not None
                else os.path.join(dest_root, "_external", os.path.basename(p)))
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            dest = _unique(dest)
            size = os.path.getsize(p)
            shutil.move(p, dest)
        except OSError as exc:
            errors.append({"path": p, "reason": str(exc)})
            continue
        moved.append({"path": p, "to": dest})
        manifest.append({"from": p, "to": dest, "size": size,
                         "moved_at": time.time()})
        with conn:                 # drop it from search straight away
            conn.execute("UPDATE tracks SET missing = 1 WHERE path = ?", (p,))

    if manifest:
        mpath = os.path.join(dest_root, "manifest.json")
        prior = []
        if os.path.isfile(mpath):
            try:
                with open(mpath, encoding="utf-8") as f:
                    prior = json.load(f)
            except (OSError, ValueError):
                prior = []
        try:
            with open(mpath, "w", encoding="utf-8") as f:
                json.dump(prior + manifest, f, indent=2)
        except OSError as exc:
            # the move already happened; just note the record failed
            errors.append({"path": mpath, "reason": f"manifest write: {exc}"})
    log.info("duplicate cleanup: moved=%d skipped=%d errors=%d",
             len(moved), len(skipped), len(errors))
    return {"moved": moved, "skipped": skipped, "errors": errors,
            "dest": dest_root if manifest else None}


# ------------------------------------------------------------------ routes

def register(app: FastAPI) -> None:
    get_conn = app.state.get_conn
    api_user = app.state.api_user
    page_user = app.state.page_user
    render = app.state.render
    cfg = app.state.cfg

    def _music_root() -> str:
        root = (cfg.get("nas_music_root") or "").strip()
        if not root:
            raise HTTPException(400, "music folder (nas_music_root) is not set")
        return root

    @app.get("/duplicates", response_class=HTMLResponse)
    def dup_page(request: Request, sess: dict = Depends(page_user)):
        return render(request, "duplicates.html", role=sess["role"])

    @app.get("/api/duplicates")
    def api_duplicates(conn=Depends(get_conn), _=Depends(api_user)):
        """READ-ONLY preview: every group of duplicate-looking files."""
        return find_duplicates(conn, _music_root())

    @app.post("/api/duplicates/quarantine")
    def api_quarantine(body: dict, conn=Depends(get_conn), _=Depends(api_user)):
        """Move the listed files into the _Duplicates folder (reversible).
        Protected files are skipped, not moved."""
        remove = body.get("remove")
        if not isinstance(remove, list) or not remove:
            raise HTTPException(400, "remove (a list of file paths) is required")
        return quarantine(conn, [str(p) for p in remove], _music_root())
