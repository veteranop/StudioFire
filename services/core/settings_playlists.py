"""Settings > Playlists: the default (everyday) rotation and the emergency playlist.

Two operator-facing slots, one page (Mark 2026-11-17, ticket M2632104):

  Default playlist   `settings.active_playlist_id` — the base rotation that is
                     always on air and that every scheduled show hands back to
                     when it ends. This is the SAME slot the Playlists page's
                     "▶ PUT ON AIR" writes; there is one truth, not two.

  Emergency playlist `settings.emergency_playlist_id` — what should play if the
                     programme runs out (missing/nas-offline/deleted tracks).
                     The engine's failover tiers read a LOCAL FOLDER, so a
                     playlist in the DB is no use to them on its own: the whole
                     point of the emergency tier is that it works when the music
                     share does not. So the chosen playlist is resolved to
                     concrete files and MIRRORED into the emergency folder
                     (atomic copies, size-capped, with a manifest) and the
                     engine's existing tier-1 plays music instead of whatever
                     spot material happened to be left in the precache.

Why this exists: John (KDPI) asked on 2026-11-17 how to load a backup playlist.
There was no such feature. His base rotation pointed at a DELETED playlist, the
emergency folder was empty, and the tier-2 fallback looped leftover PSAs/drops
on air for 1h41m (ticket 1036 / M2632102). Both defects are fixed here: the
Settings page shows BOTH slots and says so loudly when one is dangling, and
deleting a playlist can no longer leave a dangling reference behind.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import threading
import time

from fastapi import Depends, FastAPI, HTTPException

from . import db as coredb
from . import playlists as pl

log = logging.getLogger("core.settings_playlists")

# Shared with ▶ PUT ON AIR on the Playlists page — one slot, one truth.
DEFAULT_KEY = "active_playlist_id"
EMERGENCY_KEY = "emergency_playlist_id"

# Where the mirror records what IT put in the emergency folder. Hidden, and a
# non-audio extension so the engine's folder scan ignores it. Kept inside the
# emergency folder (not data/) so the copy and its receipt cannot be separated.
MANIFEST_NAME = ".studiofire-emergency.json"

# How much local disk the emergency mirror may use. Deliberately generous — a
# couple of hours of music — but bounded: this folder lives on the playout PC.
MAX_BYTES_DEFAULT = 2 * 1024 ** 3
# A belt-and-braces cap: a 20,000-item playlist must not create a 20,000-file
# folder the engine then has to list at startup.
MAX_FILES_DEFAULT = 500

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


# ------------------------------------------------------------------ helpers

def _int_or_none(raw) -> int | None:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def playlist_row(conn: sqlite3.Connection, pid: int | None):
    if not pid:
        return None
    try:
        return conn.execute(
            "SELECT id, name FROM playlists WHERE id = ?", (int(pid),)
        ).fetchone()
    except sqlite3.Error:      # pre-migration DB / closed connection
        return None


def list_playlists(conn: sqlite3.Connection) -> list[dict]:
    """Every playlist, with a track count, for the two pickers."""
    try:
        rows = conn.execute(
            "SELECT p.id AS id, p.name AS name, "
            "       (SELECT COUNT(*) FROM playlist_items i "
            "         WHERE i.playlist_id = p.id) AS tracks "
            "FROM playlists p ORDER BY LOWER(p.name)").fetchall()
    except sqlite3.Error:
        return []
    return [{"id": r["id"], "name": r["name"], "tracks": r["tracks"]}
            for r in rows]


def emergency_dir(cfg: dict | None = None) -> str:
    """The local folder the engine's failover tier reads."""
    cfg = cfg or {}
    direct = str(cfg.get("emergency_dir") or "").strip()
    if direct:
        return direct
    from .. import paths as shared_paths
    return shared_paths.resolve(cfg.get("paths") or {}, ROOT).get(
        "emergency_dir", "")


def max_bytes(cfg: dict | None = None, override: int | None = None) -> int:
    if override:
        return int(override)
    try:
        mb = int(((cfg or {}).get("core") or {}).get("emergency_mirror_max_mb")
                 or 0)
    except (TypeError, ValueError):
        mb = 0
    return mb * 1024 * 1024 if mb > 0 else MAX_BYTES_DEFAULT


def _read_manifest(dest: str) -> dict:
    try:
        with open(os.path.join(dest, MANIFEST_NAME), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_manifest(dest: str, data: dict) -> None:
    """Atomic: the engine may be listing this folder at any moment, and a
    half-written receipt would read as 'we own nothing' and orphan copies."""
    path = os.path.join(dest, MANIFEST_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def mirrored_names(dest: str) -> set[str]:
    """Files WE put in the emergency folder (per the manifest)."""
    return {str(f.get("name")) for f in (_read_manifest(dest).get("files") or [])
            if f.get("name")}


def _wanted_files(conn: sqlite3.Connection, playlist_id: int) -> list[dict]:
    """The concrete audio files a playlist stands for, in playlist order.

    `file` items contribute that one file; a folder item contributes the audio
    files inside it (sorted by name so the mirror is stable between syncs —
    folder-rotation's persisted cursor is deliberately NOT consulted: the
    emergency copy must not move a live rotation cursor).
    """
    out: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT item_type, path, title FROM playlist_items "
            "WHERE playlist_id = ? ORDER BY position, id",
            (int(playlist_id),)).fetchall()
    except sqlite3.Error:
        return out
    for row in rows:
        kind = row["item_type"]
        raw = (row["path"] or "").strip()
        if not raw:
            continue
        path = pl.alias_path(raw)
        if kind == "file":
            out.append({"src": path, "title": row["title"], "origin": "file"})
        elif kind in ("folder-newest", "folder-rotation", "folder-random"):
            for f in sorted(pl._audio_files(path),
                            key=lambda p: os.path.basename(p).lower()):
                out.append({"src": f, "title": row["title"],
                            "origin": kind})
    return out


# ------------------------------------------------------------- the two slots

def get_default(conn: sqlite3.Connection) -> dict:
    """The everyday rotation, with the dangling case made explicit.

    A stored id whose playlist no longer exists is the M2632102 defect: the GUI
    showed an empty "Now playing list" and nobody could tell why. `valid` is
    False for it, and the Settings card says so in words.
    """
    raw = coredb.get_setting(conn, DEFAULT_KEY)
    pid = _int_or_none(raw)
    row = playlist_row(conn, pid)
    return {"id": row["id"] if row else None,
            "name": row["name"] if row else None,
            "valid": bool(row),
            "dangling_id": pid if (pid and not row) else None,
            "raw": (str(raw) if raw else "")}


def set_default(conn: sqlite3.Connection, pid: int | None) -> dict:
    """Point the base rotation at a playlist (empty = no everyday rotation)."""
    if pid in (None, 0, ""):
        coredb.set_setting(conn, DEFAULT_KEY, "")
        return get_default(conn)
    row = playlist_row(conn, int(pid))
    if row is None:
        raise ValueError("that playlist no longer exists")
    coredb.set_setting(conn, DEFAULT_KEY, str(int(pid)))
    return get_default(conn)


def get_emergency(conn: sqlite3.Connection, cfg: dict | None = None) -> dict:
    raw = coredb.get_setting(conn, EMERGENCY_KEY)
    pid = _int_or_none(raw)
    row = playlist_row(conn, pid)
    return {"id": row["id"] if row else None,
            "name": row["name"] if row else None,
            "valid": bool(row),
            "dangling_id": pid if (pid and not row) else None,
            "raw": (str(raw) if raw else ""),
            "mirror": mirror_status(conn, cfg)}


def mirror_status(conn: sqlite3.Connection, cfg: dict | None = None) -> dict:
    """What is actually sitting in the emergency folder right now.

    Reads the folder, not the DB: the operator's question is "what will play if
    the music stops", and the honest answer is the files on disk.
    """
    dest = emergency_dir(cfg)
    man = _read_manifest(dest) if dest else {}
    names = [str(f.get("name")) for f in (man.get("files") or [])
             if f.get("name")]
    on_disk: list[str] = []
    curated: list[str] = []
    if dest:
        try:
            owned = set(names)
            for n in sorted(os.listdir(dest)):
                p = os.path.join(dest, n)
                if not os.path.isfile(p):
                    continue
                if os.path.splitext(n)[1].lower() not in pl.AUDIO_EXTS:
                    continue
                on_disk.append(n)
                if n not in owned:
                    curated.append(n)
        except OSError:
            pass
    current = _int_or_none(coredb.get_setting(conn, EMERGENCY_KEY))
    return {"dir": dest,
            "mirrored": len(names),
            "on_disk": len(on_disk),
            "curated": curated,
            "bytes": int(man.get("bytes") or 0),
            "max_bytes": int(man.get("max_bytes") or max_bytes(cfg)),
            "truncated": bool(man.get("truncated")),
            "playlist_id": man.get("playlist_id"),
            "playlist_name": man.get("playlist_name"),
            "synced_at": man.get("synced_at"),
            # the copy belongs to the setting only if they name the same list
            "in_sync": bool(names) and man.get("playlist_id") == current,
            "missing": list(man.get("missing") or [])[:20]}


# ----------------------------------------------------------------- the mirror

def sync_now(conn: sqlite3.Connection, playlist_id: int, cfg: dict | None = None,
             max_bytes_: int | None = None,
             max_files: int | None = None) -> dict:
    """Resolve a playlist to files and mirror it into the emergency folder.

    Best-effort by design: this runs while the station is on air, so every
    per-file failure is collected and reported, never raised. Two rules:

    1. One bad path must not empty the folder that protects the station. The
       copy is only ever pruned when this run actually resolved real audio;
       a sync that lands nothing (music share offline, all files moved) leaves
       the previous copy exactly as it was and says so.
    2. A file the operator put in the folder themselves is never touched, and
       never claimed in the manifest (so a later sync cannot delete it).
    """
    row = playlist_row(conn, playlist_id)
    if row is None:
        raise ValueError("that playlist no longer exists")
    dest = emergency_dir(cfg)
    if not dest:
        raise ValueError("no emergency folder is configured")
    os.makedirs(dest, exist_ok=True)

    budget = max_bytes(cfg, max_bytes_)
    file_cap = int(max_files or MAX_FILES_DEFAULT)
    old = _read_manifest(dest)
    old_names = {str(f.get("name")) for f in (old.get("files") or [])
                 if f.get("name")}

    try:
        raw_items = int(conn.execute(
            "SELECT COUNT(*) FROM playlist_items WHERE playlist_id = ?",
            (int(playlist_id),)).fetchone()[0])
    except sqlite3.Error:
        raw_items = 0

    wanted: list[dict] = []
    seen_names: dict[str, int] = {}
    for item in _wanted_files(conn, playlist_id):
        src = item["src"]
        if not src:
            continue
        try:
            if not os.path.isfile(src):
                continue
            size = os.path.getsize(src)
        except OSError:
            continue
        if size <= 0:
            continue
        base = os.path.basename(src)
        if base in seen_names:          # two folders, same filename
            seen_names[base] += 1
            stem, ext = os.path.splitext(base)
            base = f"{stem} ({seen_names[base]}){ext}"
        else:
            seen_names[base] = 1
        wanted.append({"name": base, "src": src, "size": size,
                       "title": item.get("title")})

    def _abort(msg: str, truncated_: bool = False) -> dict:
        """Nothing usable this run — keep the copy that is already there."""
        log.warning("emergency mirror: %s — existing copy left alone", msg)
        return {"playlist_id": row["id"], "playlist_name": row["name"],
                "dir": dest, "copied": 0, "removed": 0,
                "bytes": int(old.get("bytes") or 0),
                "max_bytes": budget, "truncated": truncated_,
                "total_wanted": 0, "missing": [msg][:20], "kept_existing": True,
                "synced_at": old.get("synced_at")}

    # The playlist has items but not one of them is readable on this machine:
    # a NAS outage looks exactly like this, and it is precisely the moment the
    # emergency copy must survive untouched.
    if raw_items and not wanted:
        return _abort(f"none of the {raw_items} item(s) could be read — is the "
                      "music folder reachable?")

    copied: list[dict] = []
    missing: list[str] = []
    kept_curated: list[str] = []
    used = 0
    truncated = False
    for w in wanted:
        target = os.path.join(dest, w["name"])
        try:
            # A same-named file we did NOT put there is the operator's own (a
            # hand-picked station ID). Never clobber it, never claim it.
            if os.path.exists(target) and w["name"] not in old_names:
                kept_curated.append(w["name"])
                continue
            if len(copied) >= file_cap or used + w["size"] > budget:
                truncated = True
                break
            if (os.path.exists(target) and w["name"] in old_names
                    and os.path.getsize(target) == w["size"]):
                copied.append({"name": w["name"], "size": w["size"]})
                used += w["size"]
                continue
            tmp = target + ".part"
            shutil.copy2(w["src"], tmp)
            os.replace(tmp, target)     # atomic: the engine never sees half a file
        except OSError as exc:
            missing.append(f"{w['name']} ({exc.__class__.__name__})")
            continue
        copied.append({"name": w["name"], "size": w["size"]})
        used += w["size"]

    if wanted and not copied:
        return _abort("nothing could be copied — the existing copy was left "
                      "alone", truncated)

    new_names = {c["name"] for c in copied}
    removed = 0
    for name in sorted(old_names - new_names):
        try:
            os.remove(os.path.join(dest, name))
            removed += 1
        except OSError:
            pass
    for junk in os.listdir(dest):
        if junk.endswith(".part"):
            try:
                os.remove(os.path.join(dest, junk))
            except OSError:
                pass

    result = {"playlist_id": row["id"], "playlist_name": row["name"],
              "dir": dest, "copied": len(copied), "removed": removed,
              "bytes": used, "max_bytes": budget,
              "truncated": truncated, "total_wanted": len(wanted),
              "kept_curated": kept_curated[:20],
              "missing": missing[:20], "synced_at": time.time()}
    try:
        _write_manifest(dest, {
            "playlist_id": row["id"], "playlist_name": row["name"],
            "synced_at": result["synced_at"], "bytes": used,
            "max_bytes": budget, "truncated": truncated,
            "files": [{"name": c["name"], "size": c["size"]}
                      for c in copied],
            "missing": missing[:20]})
    except OSError as exc:
        log.warning("emergency manifest write failed: %s", exc)
    return result


def clear_mirror(cfg: dict | None = None) -> int:
    """Wipe the files WE copied (curated audio stays). Returns the count."""
    dest = emergency_dir(cfg)
    if not dest:
        return 0
    gone = 0
    for name in sorted(mirrored_names(dest)):
        try:
            os.remove(os.path.join(dest, name))
            gone += 1
        except OSError:
            pass
    try:
        os.remove(os.path.join(dest, MANIFEST_NAME))
    except OSError:
        pass
    return gone


def set_emergency(conn: sqlite3.Connection, pid: int | None,
                  cfg: dict | None = None) -> dict:
    """Choose the emergency playlist. The copy is synced by the caller's
    worker (it can take a while over a music share); clearing wipes it now."""
    if pid in (None, 0, ""):
        coredb.set_setting(conn, EMERGENCY_KEY, "")
        clear_mirror(cfg)
        return get_emergency(conn, cfg)
    row = playlist_row(conn, int(pid))
    if row is None:
        raise ValueError("that playlist no longer exists")
    coredb.set_setting(conn, EMERGENCY_KEY, str(int(pid)))
    return get_emergency(conn, cfg)


def clear_references(conn: sqlite3.Connection, pid: int,
                     cfg: dict | None = None) -> list[str]:
    """Called when a playlist is deleted: no dangling slot may be left behind.

    M2632102 in one sentence: a playlist was deleted while it was the base
    rotation, `active_playlist_id` kept pointing at it, and the On Air page
    showed an empty slot with no explanation while the engine fell to filler.
    """
    cleared: list[str] = []
    pid = int(pid)
    if _int_or_none(coredb.get_setting(conn, DEFAULT_KEY)) == pid:
        coredb.set_setting(conn, DEFAULT_KEY, "")
        cleared.append(DEFAULT_KEY)
    if _int_or_none(coredb.get_setting(conn, EMERGENCY_KEY)) == pid:
        coredb.set_setting(conn, EMERGENCY_KEY, "")
        clear_mirror(cfg)
        cleared.append(EMERGENCY_KEY)
    return cleared


def startup_sync(conn: sqlite3.Connection, cfg: dict | None = None) -> None:
    """Best-effort: if an emergency playlist is set but its copy is missing or
    belongs to a different list, refresh it now (in the background)."""
    pid = _int_or_none(coredb.get_setting(conn, EMERGENCY_KEY))
    if not pid:
        return
    st = mirror_status(conn, cfg)
    if st["in_sync"]:
        return
    EMERGENCY_MIRROR.start(pid, cfg)


# --------------------------------------------------------------- web worker

class EmergencyMirror:
    """Copies the emergency playlist on a thread so a slow music share cannot
    hang a Settings page (the same shape as the now-playing pusher and the
    update installer: POST returns, the card polls)."""

    def __init__(self, start_timeout: float = 20.0):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = {"state": "idle", "message": "", "pid": None,
                       "finished_at": None, "result": None}

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, playlist_id: int, cfg: dict | None = None,
              db_path: str | None = None) -> bool:
        cfg = cfg or {}
        db_path = db_path or cfg.get("db_path")
        if not db_path:
            log.warning("emergency mirror: no db path, cannot start")
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._state = {"state": "copying", "message": "Copying…",
                           "pid": int(playlist_id), "finished_at": None,
                           "result": None}
        t = threading.Thread(target=self._run, name="emergency-mirror",
                             args=(int(playlist_id), dict(cfg), db_path),
                             daemon=True)
        self._thread = t
        t.start()
        return True

    def wait(self, timeout: float | None = None) -> dict:
        t = self._thread
        if t is not None:
            t.join(timeout)
        return self.status()

    def _run(self, playlist_id: int, cfg: dict, db_path: str) -> None:
        conn = None
        try:
            conn = coredb.connect(db_path)
            res = sync_now(conn, playlist_id, cfg)
            if res.get("kept_existing"):
                why = (res.get("missing") or ["nothing could be copied"])[0]
                with self._lock:
                    self._state = {"state": "error",
                                   "message": f"Kept the existing copy — {why}",
                                   "pid": playlist_id,
                                   "finished_at": time.time(),
                                   "result": res}
                return
            msg = (f"Copied {res['copied']} track(s) "
                   f"({res['bytes'] / 1048576:.0f} MB)")
            if res["truncated"]:
                msg += " — stopped at the size cap"
            with self._lock:
                self._state = {"state": "done", "message": msg,
                               "pid": playlist_id, "finished_at": time.time(),
                               "result": res}
        except Exception as exc:            # noqa: BLE001 — never kills the core
            log.exception("emergency mirror failed")
            with self._lock:
                self._state = {"state": "error", "message": str(exc),
                               "pid": playlist_id, "finished_at": time.time(),
                               "result": None}
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:               # noqa: BLE001
                pass


EMERGENCY_MIRROR = EmergencyMirror()


# ------------------------------------------------------------------ the page

def register(app: FastAPI) -> None:
    get_conn = app.state.get_conn
    api_user = app.state.api_user
    cfg = app.state.cfg

    def _body_int(body: dict, key: str):
        raw = body.get(key)
        if raw in (None, "", 0, "0", "none", "None"):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} must be a playlist id or empty")

    def _payload(conn) -> dict:
        return {"playlists": list_playlists(conn),
                "default": get_default(conn),
                "emergency": get_emergency(conn, cfg),
                "mirror_job": EMERGENCY_MIRROR.status()}

    @app.get("/api/settings/playlists")
    def api_settings_playlists(conn=Depends(get_conn), _=Depends(api_user)):
        """Both slots plus the live state of the emergency copy."""
        return _payload(conn)

    @app.post("/api/settings/playlists")
    def api_settings_playlists_save(body: dict, conn=Depends(get_conn),
                                    _=Depends(api_user)):
        """Set the default and/or emergency playlist.

        Only the keys present are touched, so the two cards can save
        independently without fighting over one another's value.
        """
        changed: list[str] = []
        try:
            if "default_playlist_id" in body:
                set_default(conn, _body_int(body, "default_playlist_id"))
                changed.append("default")
            if "emergency_playlist_id" in body:
                pid = _body_int(body, "emergency_playlist_id")
                set_emergency(conn, pid, cfg)
                changed.append("emergency")
                if pid:
                    EMERGENCY_MIRROR.start(pid, cfg)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        out = _payload(conn)
        out["changed"] = changed
        return out

    @app.post("/api/settings/playlists/emergency/refresh")
    def api_emergency_refresh(conn=Depends(get_conn), _=Depends(api_user)):
        """Re-copy the chosen playlist (after editing it, or after the share
        came back). Same job the save kicks off."""
        pid = _int_or_none(coredb.get_setting(conn, EMERGENCY_KEY))
        if not pid:
            raise HTTPException(400, "no emergency playlist is set")
        started = EMERGENCY_MIRROR.start(pid, cfg)
        out = _payload(conn)
        out["started"] = started
        return out

    return None
