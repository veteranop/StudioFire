"""P2 ⇄ P1 bridge: pre-cache feeder, manifest, queue protocol, journal ingest.

PLAN.md §10.2/§10.3/§10.4. P1 only ever plays local files listed in the
pre-cache manifest (plus its own emergency tiers). This module:

- Precache: NAS file -> temp copy -> size verify -> atomic rename into
  precache_dir, recorded in manifest.json (atomic write). A P2 death
  mid-copy leaves no visible partial file.
- Feeder: keeps ~precache_target_minutes of audio pending in P1's queue,
  resolving dynamic playlist items at feed time, wrapping the active
  playlist forever (radio never stops). Speaks the queue_version protocol;
  on 409 it re-syncs and retries. Evicts cache files after airplay.
- Journal ingest: tails P1's play_journal.jsonl into play_history,
  deduped by journal id (§10.4 — P2 downtime never loses as-aired data).

Everything here may fail at any time; P1 keeps playing regardless.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from . import db as coredb
from . import playlists as pl
from . import schedule as sched
from . import spots as spotmod

log = logging.getLogger("core.bridge")


def _read_meta(path: str) -> dict:
    """Best-effort artist/album/title/duration from a LOCAL audio file — the
    pre-cached copy, so it's fast and works even off the NAS. Never raises."""
    out = {"artist": None, "album": None, "title": None, "duration": None}
    try:
        import mutagen
        m = mutagen.File(path, easy=True)
    except Exception:  # noqa: BLE001 — mutagen raises wildly varied errors
        return out
    if m is None:
        return out
    if getattr(m, "info", None) is not None:
        out["duration"] = round(getattr(m.info, "length", 0.0) or 0.0, 2) or None
    if m.tags:
        def first(k):
            v = m.tags.get(k)
            return (str(v[0]).strip() or None) if v else None
        out["artist"] = first("artist")
        out["album"] = first("album")
        out["title"] = first("title")
    return out


DEFAULT_TRACK_SEC = 240.0   # estimate when a track's duration is unknown
FEED_TICK_SEC = 5.0
MAX_FEED_BATCH = 50         # sanity cap per tick
_AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".aac", ".wav", ".flac", ".ogg"}


# --------------------------------------------------------------- engine API

class EngineClient:
    """Thin HTTP client for P1's localhost control surface."""

    def __init__(self, base_url: str):
        self._client = httpx.Client(base_url=base_url, timeout=4.0)

    def status(self) -> dict | None:
        """None = engine unreachable (P1 may be restarting — not our problem)."""
        try:
            r = self._client.get("/status")
            return r.json() if r.status_code == 200 else None
        except httpx.HTTPError:
            return None

    def levels(self) -> dict | None:
        """Program audio level for the On Air meter (polled ~8x/sec per open
        page, so a short timeout: a slow answer is just a skipped frame)."""
        try:
            r = self._client.get("/levels", timeout=0.8)
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    def queue(self, mutation: dict) -> tuple[int, dict]:
        try:
            r = self._client.post("/queue", json=mutation)
            return r.status_code, r.json()
        except httpx.HTTPError as exc:
            return 0, {"error": str(exc)}

    def op(self, op: str) -> tuple[int, dict]:
        try:
            r = self._client.post("/op", json={"op": op})
            return r.status_code, r.json()
        except httpx.HTTPError as exc:
            return 0, {"error": str(exc)}


# ----------------------------------------------------------------- precache

class Precache:
    """§10.3: temp + verify + atomic rename; manifest lists valid items."""

    def __init__(self, precache_dir: str):
        self.dir = precache_dir
        os.makedirs(precache_dir, exist_ok=True)
        self._manifest_path = os.path.join(precache_dir, "manifest.json")
        self._manifest = self._load_manifest()
        # the feeder loop AND API request threads both touch the manifest;
        # serialize writes so os.replace can't race (Windows WinError 32)
        self._lock = threading.Lock()

    def _load_manifest(self) -> dict:
        try:
            with open(self._manifest_path, "rb") as f:
                m = json.load(f)
            if isinstance(m.get("files"), dict):
                return m
        except (OSError, ValueError):
            pass
        return {"files": {}}

    def _write_manifest(self) -> None:
        """Atomic manifest write. Caller MUST hold self._lock (so json.dump
        never iterates a dict another thread is mutating, and two threads
        never fight over the .tmp / os.replace)."""
        tmp = self._manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._manifest, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        # os.replace can transiently fail on Windows if AV/indexer briefly
        # holds the target; retry a few times before giving up.
        for attempt in range(6):
            try:
                os.replace(tmp, self._manifest_path)
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
        log.warning("precache: manifest replace kept failing; leaving tmp")
        try:
            os.remove(tmp)
        except OSError:
            pass

    def cache_path_for(self, src: str) -> str:
        digest = hashlib.sha1(
            os.path.normcase(os.path.abspath(src)).encode()).hexdigest()[:16]
        ext = os.path.splitext(src)[1].lower() or ".bin"
        return os.path.join(self.dir, digest + ext)

    def ensure(self, src: str) -> str | None:
        """Copy src into the cache (if not already valid). None on failure."""
        dst = self.cache_path_for(src)
        try:
            src_stat = os.stat(src)
        except OSError as exc:
            log.error("precache: source unreadable %s (%s)", src, exc)
            return None
        rec = self._manifest["files"].get(dst)
        if (rec and rec.get("src_size") == src_stat.st_size
                and rec.get("src_mtime") == src_stat.st_mtime
                and os.path.isfile(dst)
                and os.path.getsize(dst) == src_stat.st_size):
            return dst  # already cached and still valid
        tmp = dst + ".part"
        try:
            shutil.copyfile(src, tmp)
            if os.path.getsize(tmp) != src_stat.st_size:
                raise OSError("size mismatch after copy")
            os.replace(tmp, dst)
        except OSError as exc:
            log.error("precache copy failed %s -> %s (%s)", src, dst, exc)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return None
        with self._lock:
            self._manifest["files"][dst] = {
                "src": src, "src_size": src_stat.st_size,
                "src_mtime": src_stat.st_mtime, "cached_at": time.time()}
            self._write_manifest()
        return dst

    def evict_except(self, keep: set[str], min_age_sec: float = 600.0) -> int:
        """Drop cached files not in keep (played/abandoned). Returns count.

        A file cached more recently than min_age_sec survives regardless of
        keep — closes the window between a feeder snapshot (what to keep)
        and this call where a concurrent operator action could have just
        cached something not yet reflected in keep. Legacy manifest entries
        with no cached_at are treated as old (evictable)."""
        now = time.time()
        with self._lock:
            victims = [p for p, rec in self._manifest["files"].items()
                       if p not in keep
                       and now - rec.get("cached_at", 0) >= min_age_sec]
            for p in victims:
                try:
                    os.remove(p)
                except OSError:
                    pass
                del self._manifest["files"][p]
            if victims:
                self._write_manifest()
        return len(victims)


# ------------------------------------------------------------------- feeder

class Feeder:
    """Keeps P1's pending queue topped up from the active playlist.

    Thread safety: feeder_state (SQLite settings) is read-modify-written by
    both the background feeder loop (tick(), every 5s) and FastAPI request
    threads (operator actions — insert_spot, insert_manual, show controls,
    playlist edits). All of that is serialized through self._lock so a slow
    NAS copy mid-tick can never clobber a concurrent operator mutation and
    have _evict then delete that operator's still-queued file out from under
    P1 (the lost-update race closed by the feeder-hardening changes)."""

    def __init__(self, cfg: dict, engine: EngineClient, precache: Precache):
        self.cfg = cfg
        self.engine = engine
        self.precache = precache
        self.target_sec = cfg.get("precache_target_minutes", 45) * 60.0
        # how many tracks P1's queue itself is fed ahead of the play-head —
        # deliberately shallow; the 45-min disk cache (target_sec, above) is
        # what actually protects against a NAS outage via P1's emergency
        # filler tier reading the precache dir directly
        self.feed_ahead = max(1, int(cfg.get("feed_ahead_tracks", 3)))
        self._lock = threading.RLock()   # serializes ALL feeder_state r-m-w
        self._spot_retry: dict[int, float] = {}      # rule id -> 1st failure epoch
        self._spot_last_warn: dict[int, float] = {}  # rule id -> last warn epoch

    # feeder bookkeeping lives in settings so it survives P2 restarts
    def _load_state(self, conn) -> dict:
        raw = coredb.get_setting(conn, "feeder_state")
        try:
            st = json.loads(raw) if raw else {}
        except ValueError:
            st = {}
        st.setdefault("fed", [])            # [{id, path, duration, prog}] pending
        st.setdefault("queue_version", 0)   # last version we know of
        st.setdefault("cursor", 0)          # position in the base rotation
        # active "show" overlaying the base rotation, plays once then None:
        #   {"sched_id": int, "playlist_id": int, "cursor": int}
        st.setdefault("show", None)
        st.setdefault("now_item_id", None)  # rotation item the play-head is on
        return st

    @staticmethod
    def _now_item_id(st: dict, now_id, now_playing=None) -> int | None:
        """The playlist_item id the play-head is on, in whichever program is
        airing (base rotation or show). None during a spot/emergency.

        Match by engine id first; fall back to the currently-playing file path.
        A show's items can be fed to P1 across several batches with different
        entry ids (esp. .lst/folder shows), so the id P1 reports may no longer
        be in the feeder's tracked list — but the cached file path is stable per
        item, so it still pins the right row for the on-air highlight."""
        if now_id:
            for e in st["fed"]:
                if e["id"] == now_id:
                    return e.get("pl_item_id")  # None for spots
        if now_playing:
            # normalise: P1 reports all-backslash paths, the feeder stores the
            # precache dir with forward slashes — same file, different string
            target = os.path.normcase(os.path.normpath(now_playing))
            for e in st["fed"]:
                p = e.get("path")
                if p and os.path.normcase(os.path.normpath(p)) == target:
                    return e.get("pl_item_id")
        return None

    def _save_state(self, conn, st: dict) -> None:
        coredb.set_setting(conn, "feeder_state", json.dumps(st))

    def _duration_of(self, conn, src: str) -> float:
        row = conn.execute("SELECT duration_sec FROM tracks WHERE path = ?",
                           (src,)).fetchone()
        if row and row["duration_sec"]:
            return float(row["duration_sec"])
        return DEFAULT_TRACK_SEC

    def _next_resolved(self, conn, items: list[dict], cur: dict,
                       key: str = "cursor", wrap: bool = True):
        """Resolve the next playable item; advances cur[key].
        wrap=True: base rotation, loops forever. wrap=False: a show, returns
        None once its items are exhausted. Also None if a full lap resolves
        nothing (all sources empty/missing)."""
        n = len(items)
        if n == 0:
            return None
        for _ in range(n):
            c = cur[key]
            if c >= n:
                if not wrap:
                    return None
                c %= n
                cur[key] = c
            item = items[c]
            cur[key] = c + 1
            src = pl.resolve_item(conn, item)
            if src is not None:
                # Folder items pick a different file each time, so title by
                # the resolved file, not the item (which is the folder name).
                if item["item_type"] == "file" and item.get("title"):
                    return src, item["title"], item["id"]
                return (src, os.path.splitext(os.path.basename(src))[0],
                        item["id"])
            log.warning("feeder: item unresolvable (skip+alert, §10.5): %r",
                        item["path"])
        return None

    def activate(self, conn, playlist_id: int) -> tuple[bool, str]:
        """Make a playlist the live rotation: replace P1's queue now."""
        with self._lock:
            coredb.set_setting(conn, "active_playlist_id", str(playlist_id))
            st = self._load_state(conn)
            st["fed"], st["cursor"] = [], 0
            if st.get("show"):
                self._finish_show(conn, st)  # picking a rotation cancels any show
            self._save_state(conn, st)
            ok, why = self.tick(conn, op="replace")
        return ok, why

    # ---- scheduled/cued "shows" that interrupt the rotation (§6 Phase 3)

    def _show_items(self, conn, st: dict) -> list[dict]:
        """The active show's items. Once a show is on air its items are
        SNAPSHOT into st['show']['items'] so an operator can reorder/remove/add
        them on the fly; before that (or for a legacy overlay) they're resolved
        live from the source."""
        show = st.get("show")
        if not show:
            return []
        if show.get("items") is not None:
            return show["items"]
        return self._resolve_show_items(conn, show)

    def _resolve_show_items(self, conn, show: dict) -> list[dict]:
        """Resolve a show's source (playlist / file / .lst / folder) to items."""
        kind = show.get("kind", "playlist")
        if kind == "playlist":
            return pl.get_items(conn, show["playlist_id"])
        # a scheduled show's container path is stored in playlist_schedule
        # (often Z:\Shows\...) — resolve it so it loads without a drive mapping
        p = pl.alias_path(show.get("source_path") or "")
        if kind == "file":
            return [{"id": "f0", "item_type": "file", "path": p,
                     "title": os.path.splitext(os.path.basename(p))[0]}]
        if kind == "lst":
            return self._lst_items(p)
        if kind == "folder":
            return self._folder_items(p)
        return []

    def _folder_items(self, path: str) -> list[dict]:
        """Every audio file in a folder, filename order — a show made of
        segments (e.g. Floydian Slip). Read live so updates are picked up."""
        try:
            names = sorted(os.listdir(path))
        except OSError:
            log.warning("feeder: show folder unreadable: %r", path)
            return []
        out = []
        for i, name in enumerate(names):
            if os.path.splitext(name)[1].lower() not in _AUDIO_EXTS:
                continue
            full = os.path.join(path, name)
            if not os.path.isfile(full):
                continue
            out.append({"id": f"d{i}", "item_type": "file", "path": full,
                        "title": os.path.splitext(name)[0]})
        return out

    def _lst_items(self, path: str) -> list[dict]:
        """Parse a ZaraRadio .lst live into playable file items (path aliases
        applied, same as import)."""
        try:
            with open(path, "rb") as f:
                entries = pl.parse_lst(f.read())
        except OSError:
            log.warning("feeder: .lst show unreadable: %r", path)
            return []
        out = []
        for i, e in enumerate(entries):
            # same resolution as import: explicit aliases, then the missing-drive
            # fallback (contents are usually already UNC, but a Z:\ entry resolves)
            p = pl.alias_path(e["path"])
            out.append({"id": f"l{i}", "item_type": "file", "path": p,
                        "title": e["title"],
                        "duration_sec": e.get("duration")})
        return out

    def _clear_pending(self, conn, st: dict, status: dict) -> dict:
        """Drop P1's pending queue so a show starts at the next song boundary.
        The currently-playing song is untouched. Returns fresh engine status."""
        mutation = {"op": "clear_pending",
                    "queue_version": status["queue_version"] + 1}
        code, body = self.engine.queue(mutation)
        if code == 409:
            fresh = body.get("status") or self.engine.status() or {}
            mutation["queue_version"] = fresh.get("queue_version", 0) + 1
            code, body = self.engine.queue(mutation)
        if code == 202:
            st["queue_version"] = mutation["queue_version"]
            st["fed"] = []  # everything pending was just dropped
            return self.engine.status() or status
        log.error("feeder: clear_pending failed (%s): %s", code, body)
        return status

    def _start_show(self, conn, st: dict, entry: dict, status: dict) -> dict:
        # supersede any stray 'playing' rows so exactly one show is ever on air
        # (guards against overlay/schedule drift after restarts or double-starts)
        sched.finish_all_playing(conn, except_id=entry["id"])
        sched.set_state(conn, entry["id"], "playing")
        if (entry.get("recurrence") or "once") != "once":
            # a recurring slot has aired for today — don't fire it again today
            sched.mark_fired(conn, entry["id"], _dt.date.today().isoformat())
        show = {"sched_id": entry["id"],
                "kind": entry.get("source_kind", "playlist"),
                "playlist_id": entry.get("playlist_id"),
                "source_path": entry.get("source_path"),
                "cursor": 0}
        # snapshot the items so they can be edited on the fly during the airing
        show["items"] = self._resolve_show_items(conn, show)
        st["show"] = show
        log.warning("feeder: show '%s' on air (schedule %d) — interrupting "
                    "rotation at next boundary",
                    entry.get("name") or entry.get("playlist_name"), entry["id"])
        return self._clear_pending(conn, st, status)

    def _finish_show(self, conn, st: dict) -> None:
        show = st.get("show")
        if show:
            # retire a one-time show; re-arm a recurring one for its next slot
            sched.finish(conn, show["sched_id"])
            log.warning("feeder: show %d finished — back to the rotation",
                        show["sched_id"])
        st["show"] = None

    def _finalize_show_if_aired(self, conn, st: dict, status: dict) -> None:
        """A show stays 'playing' (banner up) until its tracks are fully fed
        AND have aired — only then hand back to the rotation. Keeps a short,
        already-queued show from being marked done while still on air."""
        show = st.get("show")
        if not show or not show.get("done_feeding"):
            return
        pending_show = any(e.get("prog") == "show" for e in st["fed"])
        if not pending_show and status.get("now_source") != "show":
            self._finish_show(conn, st)

    def _maybe_fire_scheduled(self, conn, st: dict, status: dict) -> dict:
        sched.expire_past(conn)  # retire shows past their stop date
        entry = sched.due(conn)
        if entry is None:
            return status
        cur = st.get("show")
        if cur and cur.get("sched_id") == entry["id"]:
            return status  # already airing this scheduled show
        if cur:
            # a SCHEDULED show is fixed programming — it airs at its time even
            # if another show is on air (that one ends early). Without this a
            # long show blocks every later scheduled show indefinitely.
            log.warning("feeder: scheduled show %d takes over running show %d",
                        entry["id"], cur.get("sched_id"))
            self._finish_show(conn, st)
        return self._start_show(conn, st, entry, status)

    def stop_show(self, conn) -> tuple[bool, str]:
        """Operator: end the show that's on air now and return to the rotation."""
        with self._lock:
            status = self.engine.status()
            if status is None:
                return False, "engine unreachable"
            st = self._load_state(conn)
            if not st.get("show"):
                return False, "no show is on air"
            self._finish_show(conn, st)          # mark done, clear the overlay
            self._clear_pending(conn, st, status)  # drop the show's tail
            self._save_state(conn, st)
            ok, why = self.tick(conn)            # refill from the base rotation
        return ok, f"back to the rotation ({why})"

    def start_show_now(self, conn, sched_id: int,
                       cut: bool = False) -> tuple[bool, str]:
        """Put a waiting show on air. cut=False ("Cue next"): the current song
        finishes, then the show plays. cut=True ("Start now"): the current song
        is stopped immediately and the show starts now. Either way it takes
        over whatever show is already on air."""
        with self._lock:
            status = self.engine.status()
            if status is None:
                return False, "engine unreachable"
            entry = sched.get(conn, sched_id)
            if entry is None or entry["state"] != "waiting":
                return False, "that show is not waiting to start"
            st = self._load_state(conn)
            if "pending_ids" in status:  # keep bookkeeping sane before we clear
                live = set(status["pending_ids"])
                st["fed"] = [e for e in st["fed"] if e["id"] in live]
            if st.get("show"):  # take over whatever show is already on air
                self._finish_show(conn, st)
            self._start_show(conn, st, entry, status)
            self._save_state(conn, st)
            ok, why = self.tick(conn)  # feed the show in behind the current song
            if ok and cut:
                self.engine.op("skip")  # hard cut: drop current song, show now
        return ok, f"show started ({why})"

    def resync_rotation(self, conn) -> tuple[bool, str]:
        """After a permanent edit to the active rotation playlist, rebuild the
        pre-cached buffer so the change takes effect right away. The current
        song keeps playing; everything after it is re-fed from the edited list
        starting just after wherever the play-head is."""
        with self._lock:
            status = self.engine.status()
            if status is None:
                return False, "engine unreachable"
            base_pid = coredb.get_setting(conn, "active_playlist_id")
            if not base_pid:
                return True, "no active rotation"
            items = pl.get_items(conn, int(base_pid))
            st = self._load_state(conn)
            now_id = status.get("now_id")
            cur = next((e for e in st["fed"] if e["id"] == now_id), None)
            cur_item_id = (cur.get("pl_item_id")
                           if cur and cur.get("prog") == "base"
                           else st.get("now_item_id"))
            idx_by_id = {it["id"]: i for i, it in enumerate(items)}
            if cur_item_id in idx_by_id:  # resume right after the play-head
                st["cursor"] = idx_by_id[cur_item_id] + 1
            else:                         # play-head item gone/unknown
                st["cursor"] = min(st.get("cursor", 0), len(items))
            # while a show is on air the base buffer isn't live — just fix
            # the cursor so the edit takes effect when the rotation resumes
            if st.get("show"):
                self._save_state(conn, st)
                return True, "rotation cursor updated (show on air)"
            # drop the buffer (current song keeps playing), re-feed new order
            status = self._clear_pending(conn, st, status)
            if cur is not None:
                st["fed"] = [cur]  # keep play-head so the now-marker survives
            self._save_state(conn, st)
            ok, why = self.tick(conn)
        return ok, f"re-synced ({why})"

    # ---- on-the-fly editing of the show that's on air (§ shows are editable)

    def _resync_show(self, conn, st: dict) -> tuple[bool, str]:
        """Re-feed the on-air show after its item list was edited: keep the
        current song, re-point the cursor to just after the play-head, and
        rebuild the pending buffer from the new order."""
        with self._lock:
            status = self.engine.status()
            if status is None:
                return False, "engine unreachable"
            show = st["show"]
            items = show.get("items") or []
            now_id = status.get("now_id")
            cur = next((e for e in st["fed"] if e["id"] == now_id), None)
            cur_item_id = (cur.get("pl_item_id")
                           if cur and cur.get("prog") == "show"
                           else st.get("now_item_id"))
            idx_by_id = {it["id"]: i for i, it in enumerate(items)}
            if cur_item_id in idx_by_id:
                show["cursor"] = idx_by_id[cur_item_id] + 1
            else:
                show["cursor"] = min(show.get("cursor", 0), len(items))
            show["done_feeding"] = False  # the edit may have added items back
            status = self._clear_pending(conn, st, status)
            if cur is not None:
                st["fed"] = [cur]
            self._save_state(conn, st)
            ok, why = self.tick(conn)
        return ok, f"show re-synced ({why})"

    def _ensure_show_items(self, conn, show: dict) -> None:
        """A show started before snapshots existed has no editable item list —
        materialise it from the source on first edit."""
        if show.get("items") is None:
            show["items"] = self._resolve_show_items(conn, show)

    def reorder_show(self, conn, item_ids: list) -> tuple[bool, str]:
        """Set the on-air show's item order to exactly this id sequence."""
        with self._lock:
            st = self._load_state(conn)
            show = st.get("show")
            if not show:
                return False, "no show is on air"
            self._ensure_show_items(conn, show)
            by_id = {str(it["id"]): it for it in (show.get("items") or [])}
            if {str(i) for i in item_ids} != set(by_id):
                return False, "list changed — reload the page"
            show["items"] = [by_id[str(i)] for i in item_ids]
            return self._resync_show(conn, st)

    def remove_show_item(self, conn, item_id) -> tuple[bool, str]:
        """Drop one item from the on-air show (this airing only)."""
        with self._lock:
            st = self._load_state(conn)
            show = st.get("show")
            if not show:
                return False, "no show is on air"
            self._ensure_show_items(conn, show)
            items = show.get("items") or []
            show["items"] = [it for it in items
                             if str(it["id"]) != str(item_id)]
            return self._resync_show(conn, st)

    # ---- spots: station IDs / ads / jingles / PSAs between songs (§ spots)

    def insert_spot(self, conn, folder_key: str | None = None,
                    label: str | None = None, file_path: str | None = None,
                    folder_path: str | None = None,
                    pick_mode: str | None = None) -> tuple[bool, str]:
        """Drop a spot in right after the current song (airs at the next
        boundary). Targets, in priority: a specific file, a browsed folder
        (rotate/random via pick_mode), or a legacy preset station folder.

        Resolution + precache (NAS I/O) happen BEFORE self._lock so a slow
        copy never blocks another operator or the feeder loop; only the
        queue push + bookkeeping are serialized."""
        file_path = pl.alias_path(file_path) if file_path else file_path
        folder_path = pl.alias_path(folder_path) if folder_path else folder_path
        if file_path:
            src = file_path if os.path.isfile(file_path) else None
            if src is None:
                return False, "that spot file is not there"
            title = label or os.path.splitext(os.path.basename(src))[0]
        elif folder_path:
            if not os.path.isdir(folder_path):
                return False, "that spot folder is not there"
            kind = "folder-random" if pick_mode == "random" else "folder-rotation"
            src = pl.resolve_item(conn, {"item_type": kind, "path": folder_path})
            if src is None:
                return False, "no playable files in that folder"
            title = f"{label or os.path.basename(folder_path.rstrip(chr(92) + '/'))}: " + \
                os.path.splitext(os.path.basename(src))[0]
        else:
            # legacy preset station folder (e.g. dir_station_ids = Z:\John\...):
            # resolve so a dropped Z: mapping doesn't make every legal ID / PSA
            # miss its window (KDPI 2026-09-29) — file/folder branches above
            # already go through alias_path.
            folder = pl.alias_path(coredb.get_setting(conn, folder_key))
            if not folder or not os.path.isdir(folder):
                return False, f"the {label or folder_key} folder is not set up"
            src = pl.resolve_item(conn, {"item_type": "folder-rotation",
                                         "path": folder})
            if src is None:
                return False, "no playable files in that folder"
            title = f"{label or spotmod.default_label(folder_key)}: " + \
                os.path.splitext(os.path.basename(src))[0]
        cached = self.precache.ensure(src)
        if cached is None:
            return False, "the spot file could not be cached"
        with self._lock:
            status = self.engine.status()
            if status is None:
                return False, "engine unreachable"
            entry = {"id": uuid.uuid4().hex, "path": cached, "title": title,
                     "source": "spot", "src": src}
            mutation = {"op": "insert_next",
                        "queue_version": status["queue_version"] + 1,
                        "entries": [entry]}
            code, resp = self.engine.queue(mutation)
            if code == 409:
                fresh = resp.get("status") or self.engine.status() or {}
                mutation["queue_version"] = fresh.get("queue_version", 0) + 1
                code, resp = self.engine.queue(mutation)
            if code != 202:
                return False, f"engine said {code}: {resp}"
            st = self._load_state(conn)
            st["queue_version"] = mutation["queue_version"]
            st["fed"].insert(0, {"id": entry["id"], "path": cached,
                                 "duration": self._duration_of(conn, src),
                                 "title": title, "prog": "spot",
                                 # pin until P1 confirms it: tick()'s reconcile
                                 # must not drop a just-accepted spot before it
                                 # is observed, or eviction deletes its cached
                                 # file and it never airs (KDPI 2026-09-29).
                                 # The value is the pin's start time, so a pin
                                 # that is never observed still expires
                                 # (PIN_GRACE_SEC) instead of becoming a ghost.
                                 "pinned": time.time()})
            self._save_state(conn, st)
        return True, title

    SPOT_RETRY_GRACE_SEC = 600.0   # retry a failed spot before giving up its
                                    # window — closes the ad-affidavit gap
    SPOT_WARN_INTERVAL_SEC = 60.0  # throttle the retry warning log
    # How long a just-accepted (202) spot/manual entry may stay PINNED while P1
    # hasn't reported it yet. The pin is what stops tick()'s reconcile from
    # dropping a spot before it is observed (that drop let eviction delete the
    # cached file and the spot silently never aired — KDPI 2026-09-29,
    # H2627206). But a pin must never be permanent: if P1 restarts inside that
    # window it will never report the entry, and an unexpiring pin would leave a
    # ghost in 'fed' that counts toward feed_ahead and can stop the feeder
    # topping up — a dead-air pathway. P1 observes within seconds in practice,
    # so this is many times the real window.
    PIN_GRACE_SEC = 120.0

    def insert_manual(self, conn, path: str,
                      title: str | None = None) -> tuple[bool, str]:
        """Cue a track immediately after the current song (§6 Phase 1) — the
        library search "Insert Next" action. On success `why` is the display
        title (callers that need to distinguish failure kinds should check
        the returned bool first, same contract as insert_spot)."""
        path = pl.alias_path(path)
        cached = self.precache.ensure(path)  # NAS I/O: before the lock
        if cached is None:
            return False, "file could not be read/cached"
        disp = title or os.path.splitext(os.path.basename(path))[0]
        with self._lock:
            status = self.engine.status()
            if status is None:
                return False, "engine unreachable"
            entry = {"id": uuid.uuid4().hex, "path": cached, "title": disp,
                     "source": "manual", "src": path}
            mutation = {"op": "insert_next",
                        "queue_version": status["queue_version"] + 1,
                        "entries": [entry]}
            code, resp = self.engine.queue(mutation)
            if code == 409:
                fresh = resp.get("status") or self.engine.status() or {}
                mutation["queue_version"] = fresh.get("queue_version", 0) + 1
                code, resp = self.engine.queue(mutation)
            if code != 202:
                return False, f"engine said {code}: {resp}"
            st = self._load_state(conn)
            st["fed"].insert(0, {"id": entry["id"], "path": cached,
                                 "duration": self._duration_of(conn, path),
                                 "title": disp, "prog": "manual",
                                 # pin until observed — same hazard as
                                 # insert_spot (a cued "Insert Next" track must
                                 # not be reconciled away before it airs), with
                                 # the same PIN_GRACE_SEC expiry
                                 "pinned": time.time()})
            self._save_state(conn, st)
        return True, disp

    def _pin_expired(self, entry: dict, now: float) -> bool:
        """True when a PINNED entry has outlived PIN_GRACE_SEC without P1 ever
        reporting it — i.e. P1 restarted (or dropped it) inside the window, so
        the pin will never be released by observation. Returning True lets the
        caller drop it: an unexpiring pin would leave a ghost in 'fed' that
        counts toward feed_ahead and can stop the feeder topping up (a dead-air
        pathway), and would hold its cached file forever. Say so loudly — an
        accepted item that never appeared is worth knowing about."""
        started = entry.get("pinned")
        if not started or now - float(started) <= self.PIN_GRACE_SEC:
            return False
        log.warning("feeder: P1 never reported a queued %s (%r) within %.0fs — "
                    "releasing the pin and dropping it from the feed model; it "
                    "may not have aired", entry.get("prog") or "item",
                    (entry.get("title") or "")[:60], self.PIN_GRACE_SEC)
        return True

    def fire_due_spots(self, conn) -> None:
        """Called every tick: fire any spot rule that is due (all trigger
        types except manual). Spots play everywhere, shows included.

        A failed insert (e.g. a transient NAS blip) doesn't immediately give
        up the rule's window: the rule stays due and retries for up to
        SPOT_RETRY_GRACE_SEC before mark_fired is finally called, so a
        genuine ad/legal-ID doesn't silently miss its slot on one bad tick.
        Each retry goes through insert_spot, which re-enters self._lock —
        safe, it's an RLock."""
        status = self.engine.status()
        if status is None or not status.get("now_playing") \
                or status.get("emergency_mode"):
            return  # nothing airing / in failover — don't inject
        now = time.time()
        for rule in spotmod.list_enabled(conn):
            if rule["trigger"] == "manual" or not spotmod.due(rule, now):
                continue
            ok, why = self.insert_spot(
                conn, rule["folder_key"], rule["label"],
                file_path=rule.get("file_path"),
                folder_path=rule.get("folder_path"),
                pick_mode=rule.get("pick_mode"))
            rid = rule["id"]
            if ok:
                spotmod.mark_fired(conn, rule, now)
                self._spot_retry.pop(rid, None)
                self._spot_last_warn.pop(rid, None)
                log.info("spot fired (%s): %s",
                         rule.get("file_path") or rule["folder_key"], why)
                continue
            first_fail = self._spot_retry.setdefault(rid, now)
            if now - first_fail < self.SPOT_RETRY_GRACE_SEC:
                last_warn = self._spot_last_warn.get(rid, 0.0)
                if now - last_warn >= self.SPOT_WARN_INTERVAL_SEC:
                    self._spot_last_warn[rid] = now
                    log.warning("spot rule %d failed, retrying within its "
                               "window: %s", rid, why)
                continue  # stays due — no mark_fired; tries again next tick
            spotmod.mark_fired(conn, rule, now)
            self._spot_retry.pop(rid, None)
            self._spot_last_warn.pop(rid, None)
            log.error("spot rule %d MISSED its window after %ds of retries: "
                      "%s", rid, int(self.SPOT_RETRY_GRACE_SEC), why)

    def tick(self, conn, op: str = "append") -> tuple[bool, str]:
        """Reconcile with P1, feed it up to self.feed_ahead tracks, and (on a
        successful top-up/feed) run the cache lookahead + eviction AFTER
        releasing self._lock — that part does NAS I/O and must never block
        an operator action or the next tick (§10.3, feeder hardening)."""
        evict_keep = None
        evict_snapshot = None
        with self._lock:
            status = self.engine.status()
            if status is None:
                return False, "engine unreachable"

            st = self._load_state(conn)
            # reconcile: keep entries P1 still has pending, PLUS the one
            # currently playing (so we can map the play-head back to a
            # playlist item for the rotation view's "now" marker). Count
            # fallback if no pending_ids.
            now_id = status.get("now_id")
            if op == "replace":
                st["fed"] = []
            elif "pending_ids" in status:
                keep = set(status["pending_ids"])
                if now_id:
                    keep.add(now_id)
                # Never drop a just-accepted (202) insert before P1 has been
                # seen to hold it: a spot/manual entry is 'pinned' when queued
                # and only unpinned once it shows up in this reconcile (pending
                # or on air). A status snapshot that doesn't list it yet must
                # NOT reconcile it away — that dropped it from 'fed', the
                # eviction pass then deleted its cached file, and the spot
                # silently never aired (KDPI 2026-09-29, H2627206). Once
                # observed, normal reconcile retires it after it airs/ends.
                kept = []
                now = time.time()
                for e in st["fed"]:
                    if e["id"] in keep:
                        e.pop("pinned", None)   # observed -> released
                        kept.append(e)
                    elif e.get("pinned") and not self._pin_expired(e, now):
                        kept.append(e)          # accepted, not yet observed
                st["fed"] = kept
            else:
                # legacy engine with no pending_ids: trim played entries from
                # the front, but keep any pinned (not-yet-observed) insert
                pending_count = max(0, status["queue_len"]
                                    - status["current_index"] - 1)
                now = time.time()
                pinned = [e for e in st["fed"] if e.get("pinned")
                          and not self._pin_expired(e, now)]
                rest = [e for e in st["fed"] if not e.get("pinned")]
                if len(rest) > pending_count:
                    rest = rest[len(rest) - pending_count:]
                st["fed"] = pinned + rest
            st["now_item_id"] = self._now_item_id(st, now_id,
                                                  status.get("now_playing"))

            # a show that has fully aired hands back to the rotation
            self._finalize_show_if_aired(conn, st, status)
            # a scheduled show whose time has come interrupts the rotation
            if op != "replace":
                status = self._maybe_fire_scheduled(conn, st, status)
            # keep the schedule's 'playing' flags in lockstep with the
            # overlay: exactly the on-air show (if any) stays 'playing';
            # strays are retired (a stray would wrongly display as SHOW ON
            # AIR)
            _show = st.get("show")
            sched.finish_all_playing(
                conn, except_id=_show["sched_id"] if _show else None)

            base_pid = coredb.get_setting(conn, "active_playlist_id")
            base_items = pl.get_items(conn, int(base_pid)) if base_pid else []
            if not base_items and not st.get("show"):
                self._save_state(conn, st)
                return True, "no active playlist"

            # the currently-playing entry is kept in fed for the now-marker
            # but is not "pending work" — don't count it toward feed_ahead
            pending = [e for e in st["fed"] if e["id"] != now_id]
            if pending and len(pending) >= self.feed_ahead:
                self._save_state(conn, st)
                evict_keep, evict_snapshot = self._pre_evict_snapshot(
                    st, status)
                result = (True, "topped up")
            else:
                # build a batch up to feed_ahead tracks deep: the active show
                # plays once through first, then the base rotation carries on
                # forever. P1's queue is fed shallow on purpose — the disk
                # cache lookahead below is what carries the real NAS-outage
                # buffer (feeder hardening: feed depth != cache depth)
                show_items = self._show_items(conn, st)
                batch = []
                cache_fails = 0
                while len(pending) + len(batch) < self.feed_ahead \
                        and len(batch) < MAX_FEED_BATCH:
                    show = st.get("show")
                    if show and not show.get("done_feeding"):
                        resolved = self._next_resolved(conn, show_items, show,
                                                       wrap=False)
                        if resolved is None:  # fully fed -> fill with base
                            show["done_feeding"] = True
                            show_items = []
                            continue
                        prog, source = "show", "show"
                    else:
                        if not base_items:
                            break
                        resolved = self._next_resolved(conn, base_items, st,
                                                        wrap=True)
                        if resolved is None:
                            break
                        prog, source = "base", "playlist"
                    src, title, item_id = resolved
                    cached = self.precache.ensure(src)
                    if cached is None:
                        cache_fails += 1
                        if cache_fails >= max(1, len(base_items)
                                              + len(show_items)):
                            break  # NAS is gone — stop burning the tick
                        continue  # source vanished mid-feed; try the next
                    # read real artist/album/title/duration from the cache
                    meta = _read_meta(cached)
                    song = meta["title"] or title       # tag title, else file
                    artist, album = meta["artist"], meta["album"]
                    duration = meta["duration"] or self._duration_of(conn, src)
                    # remember the real length on the playlist item (feeds the
                    # song count / run time + time-left displays). Int ids are
                    # playlist_items rows; .lst/folder show items use string ids
                    if isinstance(item_id, int) and meta["duration"]:
                        pl.set_duration_if_unknown(conn, item_id,
                                                   meta["duration"])
                    disp = f"{artist} - {song}" if artist else song
                    eid = uuid.uuid4().hex
                    batch.append({"id": eid, "path": cached, "title": disp,
                                  "source": source, "src": src})
                    st["fed"].append({"id": eid, "path": cached,
                                      "duration": duration, "title": disp,
                                      "prog": prog,
                                      # the playlist item this came from (base
                                      # OR show), so the on-air list can mark
                                      # the current song in whichever program
                                      # is playing
                                      "pl_item_id": item_id,
                                      # full metadata for Now Playing
                                      "artist": artist, "album": album,
                                      "song": song})
                if not batch and op != "replace":
                    self._save_state(conn, st)
                    return True, "nothing to feed"

                mutation = {"op": op,
                            "queue_version": status["queue_version"] + 1,
                            "entries": batch}
                code, body = self.engine.queue(mutation)
                if code == 409:  # version bumped elsewhere — re-sync, retry
                    fresh = body.get("status") or self.engine.status() or {}
                    mutation["queue_version"] = fresh.get(
                        "queue_version", 0) + 1
                    code, body = self.engine.queue(mutation)
                if code != 202:
                    # roll back bookkeeping for the rejected batch
                    fed_ids = {b["id"] for b in batch}
                    st["fed"] = [e for e in st["fed"] if e["id"] not in fed_ids]
                    self._save_state(conn, st)
                    return False, f"queue push failed ({code}): {body}"
                st["queue_version"] = mutation["queue_version"]
                self._save_state(conn, st)
                evict_keep, evict_snapshot = self._pre_evict_snapshot(
                    st, status)
                result = (True, f"fed {len(batch)} entries")

        # NAS/disk I/O deliberately outside self._lock (§10.3, feeder
        # hardening): a slow copy here must never block an operator action.
        keep = evict_keep | self._cache_lookahead(conn, evict_snapshot)
        n = self.precache.evict_except(keep)
        if n:
            log.info("precache: evicted %d played file(s)", n)
        return result

    def _pre_evict_snapshot(self, st: dict, status: dict
                            ) -> tuple[set[str], dict]:
        """Called with self._lock held: capture what must survive eviction
        (currently-fed paths + now playing) and a deep-copied state snapshot
        for _cache_lookahead to simulate forward from without touching the
        real (still-locked) st."""
        keep = {e["path"] for e in st["fed"]}
        now_playing = status.get("now_playing")
        if now_playing:
            keep.add(now_playing)
        return keep, json.loads(json.dumps(st))

    @staticmethod
    def _peek_item(items: list[dict], cur: dict, key: str = "cursor",
                   wrap: bool = True) -> dict | None:
        """Like _next_resolved but returns the raw item — never calls
        pl.resolve_item, so a speculative lookahead can never trigger
        folder-rotation's persisted-cursor side effect or burn a random pick
        that the real feed won't use. Advances cur[key] by exactly one."""
        n = len(items)
        if n == 0:
            return None
        c = cur[key]
        if c >= n:
            if not wrap:
                return None
            c %= n
        item = items[c]
        cur[key] = c + 1
        return item

    def _cache_lookahead(self, conn, st_snapshot: dict) -> set[str]:
        """Pre-copy upcoming rotation material to the precache dir WITHOUT
        consuming the real cursor (operates on the deep-copied snapshot).
        Returns the set of cache paths to protect from eviction. Runs
        OUTSIDE self._lock — only touches the snapshot and the DB/NAS.

        This is what actually carries ~precache_target_minutes of real music
        on disk even though P1's own queue is only fed self.feed_ahead
        tracks deep (see tick()): P1's emergency filler tier reads real
        rotation music straight from the precache dir when it can't reach
        the queue feed at all, so this buffer IS the NAS-outage protection."""
        keep: set[str] = set()
        try:
            show = st_snapshot.get("show")
            show_items = self._show_items(conn, st_snapshot) if show else []
            base_pid = coredb.get_setting(conn, "active_playlist_id")
            base_items = pl.get_items(conn, int(base_pid)) if base_pid else []
            pending_sec = 0.0
            scanned = 0
            max_scan = MAX_FEED_BATCH * 2  # runaway guard, pathological lists
            while pending_sec < self.target_sec and scanned < max_scan:
                scanned += 1
                if show and not show.get("done_feeding") and show_items:
                    item = self._peek_item(show_items, show, wrap=False)
                    if item is None:
                        show = None  # snapshot's show exhausted -> base
                        continue
                else:
                    if not base_items:
                        break
                    item = self._peek_item(base_items, st_snapshot, wrap=True)
                    if item is None:
                        break
                if item["item_type"] != "file":
                    # non-deterministic (folder-rotation/random/newest) —
                    # resolving here would either mutate real rotation-cursor
                    # state or just be a wasted guess; count an estimate and
                    # move on so the lookahead depth stays roughly honest
                    pending_sec += DEFAULT_TRACK_SEC
                    continue
                # same aliasing as the real feed (resolve_item), so the cache
                # key matches what tick() will look up
                src = pl.alias_path(item["path"])
                if not os.path.isfile(src):
                    continue
                cached = self.precache.ensure(src)
                if cached is None:
                    continue
                keep.add(cached)
                pending_sec += self._duration_of(conn, src)
        except Exception:
            log.exception("feeder: cache lookahead failed (non-fatal)")
        return keep


# ----------------------------------------------------------- journal ingest

def ingest_journal(conn: sqlite3.Connection, journal_path: str) -> int:
    """Tail P1's journal into play_history, deduped by journal id."""
    state_raw = coredb.get_setting(conn, "journal_ingest")
    try:
        state = json.loads(state_raw) if state_raw else {}
    except ValueError:
        state = {}
    offset = int(state.get("offset", 0))
    ingested = 0
    try:
        size = os.path.getsize(journal_path)
    except OSError:
        return 0  # engine hasn't written yet
    if size < offset:
        # active file rotated out from under us: re-ingest rotated siblings
        # (INSERT OR IGNORE makes this idempotent), then restart at 0
        base, ext = os.path.splitext(journal_path)
        folder = os.path.dirname(journal_path)
        prefix = os.path.basename(base) + "."
        for name in sorted(os.listdir(folder)):
            if name.startswith(prefix) and name.endswith(ext):
                ingested += _ingest_lines(conn,
                                          os.path.join(folder, name), 0)[0]
        offset = 0
    n, offset = _ingest_lines(conn, journal_path, offset)
    ingested += n
    coredb.set_setting(conn, "journal_ingest", json.dumps({"offset": offset}))
    return ingested


def _ingest_lines(conn, path: str, offset: int) -> tuple[int, int]:
    count = 0
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # torn tail — pick it up next pass
                offset += len(raw)
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if "id" not in ev or "event" not in ev:
                    continue
                extra = {k: v for k, v in ev.items()
                         if k not in ("id", "ts", "event", "path", "title",
                                      "source")}
                with conn:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO play_history "
                        "  (journal_id, ts, event, path, title, source, extra) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (ev["id"], ev.get("ts", ""), ev["event"],
                         ev.get("path"), ev.get("title"), ev.get("source"),
                         json.dumps(extra) if extra else None))
                count += cur.rowcount
    except OSError:
        pass
    return count, offset


# ---------------------------------------------------------------- wiring

class PlayNextIn(BaseModel):
    path: str
    title: str | None = None


def register(app: FastAPI) -> None:
    cfg = app.state.cfg
    get_conn = app.state.get_conn
    api_user = app.state.api_user

    engine = EngineClient(cfg["engine_url"])
    precache = Precache(cfg["precache_dir"])
    feeder = Feeder(cfg, engine, precache)
    app.state.engine = engine
    app.state.feeder = feeder

    @app.get("/api/engine/status")
    def api_engine_status(_=Depends(api_user)):
        st = engine.status()
        return {"engine_online": st is not None, **(st or {})}

    @app.get("/api/levels")
    def api_levels(_=Depends(api_user)):
        """Live program level (dBFS per channel, None = silence) for the On
        Air VU meter. ok=False when the engine can't be reached."""
        lv = engine.levels()
        return {"ok": lv is not None, **(lv or {})}

    @app.post("/api/engine/op")
    def api_engine_op(body: dict, _=Depends(api_user)):
        op = body.get("op", "")
        if op not in ("pause", "resume", "skip", "stop_after",
                      "emergency", "resume_normal"):
            raise HTTPException(
                400, "op must be pause/resume/skip/stop_after/"
                     "emergency/resume_normal")
        code, resp = engine.op(op)
        if code != 200:
            raise HTTPException(502, f"engine said {code}: {resp}")
        return resp

    @app.get("/api/queue")
    def api_queue(conn=Depends(get_conn), _=Depends(api_user)):
        """Now playing + the pending titles the feeder has queued into P1."""
        st = engine.status()
        now_id = (st or {}).get("now_id")
        with feeder._lock:  # consistent snapshot of feeder_state vs a live tick
            fst = feeder._load_state(conn)
            pending = fst["fed"]
            # the currently-playing fed entry carries full artist/album/song
            now_e = next((e for e in pending if e["id"] == now_id), None) \
                if now_id else None
            if st is not None and "pending_ids" in st:
                order = {i: k for k, i in enumerate(st["pending_ids"])}
                pending = sorted((e for e in pending if e["id"] in order),
                                 key=lambda e: order[e["id"]])
            elif st is not None:
                n = max(0, st["queue_len"] - st["current_index"] - 1)
                if len(pending) > n:
                    pending = pending[len(pending) - n:]
            pending_view = [{"id": e["id"],
                             "title": e.get("title") or "(untitled)",
                             "duration": e.get("duration")}
                            for e in pending]
            now_artist = (now_e or {}).get("artist")
            now_album = (now_e or {}).get("album")
            now_song = (now_e or {}).get("song")
        return {"engine_online": st is not None,
                "now_playing": (st or {}).get("now_playing"),
                "now_title": (st or {}).get("now_title"),
                "now_artist": now_artist,
                "now_album": now_album,
                "now_song": now_song,
                "now_source": (st or {}).get("now_source"),
                "duration": (st or {}).get("duration"),
                "position": (st or {}).get("position"),
                "paused": (st or {}).get("paused", False),
                "stop_after_current": (st or {}).get("stop_after_current", False),
                "emergency_mode": (st or {}).get("emergency_mode", False),
                "forced_emergency": (st or {}).get("forced_emergency", False),
                "pending": pending_view}

    def _queue_mutate(mutation_body: dict) -> dict:
        """Submit a queue mutation with the version protocol + 409 re-sync.
        `mutation_body` is everything but queue_version. Raises HTTPException."""
        status = engine.status()
        if status is None:
            raise HTTPException(502, "engine unreachable")
        mutation = {**mutation_body,
                    "queue_version": status["queue_version"] + 1}
        code, resp = engine.queue(mutation)
        if code == 409:  # feeder bumped the version underneath us — retry
            fresh = resp.get("status") or engine.status() or {}
            mutation["queue_version"] = fresh.get("queue_version", 0) + 1
            code, resp = engine.queue(mutation)
        if code != 202:
            raise HTTPException(502, f"engine said {code}: {resp}")
        return resp

    @app.post("/api/queue/reorder")
    def api_queue_reorder(body: dict, _=Depends(api_user)):
        """Drag-to-reorder: `order` is the desired pending id sequence."""
        order = body.get("order")
        if not isinstance(order, list):
            raise HTTPException(400, "order must be a list of ids")
        _queue_mutate({"op": "reorder", "order": order})
        return {"ok": True}

    @app.post("/api/queue/remove")
    def api_queue_remove(body: dict, _=Depends(api_user)):
        qid = body.get("id")
        if not qid:
            raise HTTPException(400, "id required")
        _queue_mutate({"op": "remove", "ids": [qid]})
        return {"ok": True}

    @app.post("/api/queue/cue_next")
    def api_queue_cue_next(body: dict, _=Depends(api_user)):
        """Jump a pending track to the front of the queue (plays next)."""
        qid = body.get("id")
        if not qid:
            raise HTTPException(400, "id required")
        _queue_mutate({"op": "reorder", "order": [qid]})
        return {"ok": True}

    @app.post("/api/queue/play_now")
    def api_queue_play_now(body: dict, _=Depends(api_user)):
        """Cut to a pending track immediately: move it next, then skip."""
        qid = body.get("id")
        if not qid:
            raise HTTPException(400, "id required")
        _queue_mutate({"op": "reorder", "order": [qid]})
        code, resp = engine.op("skip")
        if code != 200:
            raise HTTPException(502, f"engine said {code}: {resp}")
        return {"ok": True}

    @app.post("/api/playlists/{pid}/activate")
    def api_activate(pid: int, conn=Depends(get_conn), _=Depends(api_user)):
        row = conn.execute("SELECT id FROM playlists WHERE id = ?",
                           (pid,)).fetchone()
        if row is None:
            raise HTTPException(404, "playlist not found")
        ok, why = feeder.activate(conn, pid)
        if not ok:
            raise HTTPException(502, why)
        return {"ok": True, "detail": why}

    # ---------------------------------- the live rotation playlist (editable)

    @app.get("/api/rotation")
    def api_rotation(conn=Depends(get_conn), _=Depends(api_user)):
        """The playlist that's actually on air in full + the on-air song.
        A show overrides the base rotation while it plays; the list follows
        whatever is airing so it always matches Now Playing. It's read-only
        while a show is on (you edit your rotation, not a one-time show)."""
        with feeder._lock:  # consistent snapshot of feeder_state vs a live tick
            st = feeder._load_state(conn)
            estatus = engine.status() or {}
            now_eid = estatus.get("now_id")
            now_item_id = feeder._now_item_id(st, now_eid,
                                              estatus.get("now_playing"))

            def _fmt(items, durs=None):
                """durs: {item_id: sec} for a base playlist; show items carry
                their own duration_sec (or fall back to the music index)."""
                out = []
                for it in items:
                    dur = (durs or {}).get(it["id"]) or it.get("duration_sec")
                    if not dur and it["item_type"] == "file" and durs is None:
                        row = conn.execute(
                            "SELECT duration_sec FROM tracks WHERE path = ?",
                            (it["path"],)).fetchone()
                        dur = row["duration_sec"] if row else None
                    out.append({"id": it["id"], "item_type": it["item_type"],
                                "title": it["title"] or os.path.splitext(
                                    os.path.basename(it["path"]))[0],
                                "duration": dur or None})
                return out

            def _timing(items):
                """Song count, total run time, and how much is left AFTER the
                on-air song (the page adds the on-air song's own time left).
                A rotation loops forever, so 'left' means left in this pass.
                `*_unknown` = songs with no known length (not in the sums)."""
                songs = [i for i in items if i["item_type"] != "insert"]
                at = next((k for k, i in enumerate(songs)
                           if i["id"] == now_item_id), None)
                after = songs[at + 1:] if at is not None else []
                return {"count": len(songs),
                        "total_sec": round(sum(i["duration"] or 0
                                               for i in songs), 1),
                        "total_unknown": sum(1 for i in songs
                                             if not i["duration"]),
                        "left_after_now_sec": (round(sum(
                            i["duration"] or 0 for i in after), 1)
                            if at is not None else None),
                        "left_unknown": sum(1 for i in after
                                            if not i["duration"]),
                        "songs_after_now": len(after)
                        if at is not None else None}

            def _with_inserts(items):
                """Splice cued one-offs (library 'Insert Next' + spots) into
                the on-air list right after the current song, so a DJ sees
                what they just queued instead of it being invisible in the
                engine queue."""
                inserts = [
                    {"id": e["id"], "item_type": "insert",
                     "kind": "spot" if e.get("prog") == "spot" else "cued",
                     "title": e.get("title") or "…"}
                    for e in st.get("fed", [])
                    if e["id"] != now_eid
                    and e.get("prog") in ("manual", "spot")]
                if not inserts:
                    return items
                at = next((i for i, it in enumerate(items)
                           if it["id"] == now_item_id), -1)
                for off, ins in enumerate(inserts):
                    items.insert(at + 1 + off, ins)
                return items

            if st.get("show"):  # a show is on air — follow it
                cur = sched.playing(conn)
                name = (cur.get("name") if cur else None) or "Show on air"
                items = _fmt(feeder._show_items(conn, st))
                return {"playlist": {"id": None, "name": name},
                        "is_show": True, "now_item_id": now_item_id,
                        "timing": _timing(items),
                        "items": _with_inserts(items)}
            base = coredb.get_setting(conn, "active_playlist_id")
            row = conn.execute("SELECT id, name FROM playlists WHERE id = ?",
                               (int(base),)).fetchone() if base else None
            if row is None:
                return {"playlist": None, "items": [], "now_item_id": None,
                        "is_show": False, "timing": None}
            items = _fmt(pl.get_items(conn, row["id"]),
                         pl.item_durations(conn, row["id"]))
            return {"playlist": {"id": row["id"], "name": row["name"]},
                    "is_show": False, "now_item_id": now_item_id,
                    "timing": _timing(items),
                    "items": _with_inserts(items)}

    @app.get("/api/history")
    def api_history(limit: int = 40, conn=Depends(get_conn),
                    _=Depends(api_user)):
        """As-aired log: the most recent track starts/ends from play_history."""
        rows = conn.execute(
            "SELECT ts, event, title, source, path FROM play_history "
            "WHERE event = 'track_start' "  # what aired; titles are Artist - Song
            "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        out = []
        for r in rows:
            title = r["title"] or (os.path.splitext(
                os.path.basename(r["path"]))[0] if r["path"] else "—")
            out.append({"ts": r["ts"], "event": r["event"],
                        "title": title, "source": r["source"]})
        return out

    def _active_pid(conn) -> int:
        base_pid = coredb.get_setting(conn, "active_playlist_id")
        if not base_pid:
            raise HTTPException(409, "no rotation is on air")
        return int(base_pid)

    @app.post("/api/rotation/reorder")
    def api_rotation_reorder(body: dict, conn=Depends(get_conn),
                             _=Depends(api_user)):
        item_ids = body.get("item_ids")
        if not isinstance(item_ids, list):
            raise HTTPException(400, "item_ids must be a list")
        # a show on air is edited live (this airing only); otherwise the base
        # rotation playlist is edited permanently
        if feeder._load_state(conn).get("show"):
            ok, why = feeder.reorder_show(conn, item_ids)
            if not ok:
                raise HTTPException(409, why)
            return {"ok": True}
        pid = _active_pid(conn)
        try:
            ids = [int(i) for i in item_ids]
        except (TypeError, ValueError):
            raise HTTPException(400, "bad item ids")
        existing = {i["id"] for i in pl.get_items(conn, pid)}
        if set(ids) != existing:
            raise HTTPException(409, "list changed — reload the page")
        pl.reorder_items(conn, pid, ids)          # permanent edit
        feeder.resync_rotation(conn)              # take effect on air now
        return {"ok": True}

    @app.post("/api/rotation/shuffle")
    def api_rotation_shuffle(conn=Depends(get_conn), _=Depends(api_user)):
        """Randomly reorder the on-air rotation — the On-Air Playlist card's
        Shuffle button. A permanent edit to the active playlist, re-synced so
        it takes effect on air now (the current song is never interrupted).
        Refused while a show is on air: the card is read-only then, and the
        rotation resumes when the show ends."""
        if feeder._load_state(conn).get("show"):
            raise HTTPException(409, "a show is on air — the rotation shuffles "
                                     "when it ends")
        pid = _active_pid(conn)
        ids = pl.shuffle_items(conn, pid)
        feeder.resync_rotation(conn)
        return {"ok": True, "item_ids": ids}

    @app.post("/api/rotation/remove")
    def api_rotation_remove(body: dict, conn=Depends(get_conn),
                            _=Depends(api_user)):
        item_id = body.get("item_id")
        if item_id is None:
            raise HTTPException(400, "item_id required")
        if feeder._load_state(conn).get("show"):
            ok, why = feeder.remove_show_item(conn, item_id)
            if not ok:
                raise HTTPException(409, why)
            return {"ok": True}
        pid = _active_pid(conn)
        try:
            item_id = int(item_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "item_id required")
        pl.remove_item(conn, pid, item_id)        # permanent edit
        feeder.resync_rotation(conn)              # take effect on air now
        return {"ok": True}

    # ---------------------------------------- playlist schedule (shows)

    @app.get("/api/schedule")
    def api_schedule(conn=Depends(get_conn), _=Depends(api_user)):
        """What's the base rotation, is a show on air, and what's queued."""
        base = None
        base_pid = coredb.get_setting(conn, "active_playlist_id")
        if base_pid:
            row = conn.execute("SELECT id, name FROM playlists WHERE id = ?",
                               (int(base_pid),)).fetchone()
            if row:
                base = {"id": row["id"], "name": row["name"]}
        cur = sched.playing(conn)
        return {
            "base": base,
            "current_show": ({"sched_id": cur["id"], "name": cur["name"]}
                             if cur else None),
            "now": sched.now_local(),
            "upcoming": [{"id": e["id"], "name": e["name"],
                          "source_kind": e["source_kind"],
                          "start_at": e["start_at"],
                          "recurrence": e.get("recurrence") or "once",
                          "end_date": e.get("end_date"),
                          "when": e["when"],
                          "next_at": e["next_at"],
                          "next_label": e["next_label"]}
                         for e in sched.list_waiting(conn)],
        }

    def _sched_timing(body: dict) -> dict:
        """Validate the when-to-play half of a schedule request into kwargs for
        sched.add: a one-time start_at, or a daily/weekly recurring slot with an
        optional run window (start_date .. end_date, the 'stop date')."""
        rec = (body.get("recurrence") or "once").strip()
        if rec not in ("once", "daily", "weekly"):
            raise HTTPException(400, "recurrence must be once, daily or weekly")
        if rec == "once":
            start_at = (body.get("start_at") or "").strip() or None
            if start_at and len(start_at) < 16:  # 'YYYY-MM-DDTHH:MM'
                raise HTTPException(400, "start time must be YYYY-MM-DDTHH:MM")
            return {"recurrence": "once", "start_at": start_at}
        tod = (body.get("time_of_day") or "").strip()
        try:
            _dt.datetime.strptime(tod, "%H:%M")
        except ValueError:
            raise HTTPException(400, "time of day must be HH:MM")
        mask = None
        if rec == "weekly":
            try:
                mask = int(body.get("days_mask") or 0)
            except (TypeError, ValueError):
                mask = 0
            if not mask:
                raise HTTPException(400, "pick at least one weekday")
        sd = (body.get("start_date") or "").strip() or None
        ed = (body.get("end_date") or "").strip() or None
        for d in (sd, ed):
            if d is not None:
                try:
                    _dt.date.fromisoformat(d)
                except ValueError:
                    raise HTTPException(400, "dates must be YYYY-MM-DD")
        if sd and ed and ed < sd:
            raise HTTPException(400, "stop date is before the start date")
        return {"recurrence": rec, "time_of_day": tod, "days_mask": mask,
                "start_date": sd, "end_date": ed}

    @app.post("/api/schedule", status_code=201)
    def api_schedule_add(body: dict, conn=Depends(get_conn),
                         _=Depends(api_user)):
        """Schedule a show: a playlist, a single audio file, or a .lst file,
        one-time or recurring (daily/weekly) with an optional stop date."""
        kind = body.get("source_kind") or (
            "playlist" if body.get("playlist_id") is not None else None)
        timing = _sched_timing(body)
        if kind == "playlist":
            try:
                pid = int(body.get("playlist_id"))
            except (TypeError, ValueError):
                raise HTTPException(400, "playlist_id required")
            if conn.execute("SELECT 1 FROM playlists WHERE id = ?",
                            (pid,)).fetchone() is None:
                raise HTTPException(404, "playlist not found")
            return {"id": sched.add(conn, "playlist", playlist_id=pid, **timing)}
        if kind in ("file", "lst"):
            path = (body.get("source_path") or "").strip()
            # validate against the resolved path (a Z:\ path that maps is valid);
            # store the raw value so it stays portable — resolve, don't rewrite
            if not path or not os.path.isfile(pl.alias_path(path)):
                raise HTTPException(400, "that file does not exist")
            if kind == "lst" and not path.lower().endswith(".lst"):
                raise HTTPException(400, "that isn't a .lst file")
            return {"id": sched.add(conn, kind, source_path=path, **timing)}
        if kind == "folder":
            path = (body.get("source_path") or "").strip()
            if not path or not os.path.isdir(pl.alias_path(path)):
                raise HTTPException(400, "that folder does not exist")
            return {"id": sched.add(conn, "folder", source_path=path, **timing)}
        raise HTTPException(400,
                            "source_kind must be playlist, file, lst or folder")

    @app.delete("/api/schedule/{sid}")
    def api_schedule_remove(sid: int, conn=Depends(get_conn),
                            _=Depends(api_user)):
        sched.remove(conn, sid)
        return {"ok": True}

    @app.post("/api/schedule/{sid}/start_now")
    def api_schedule_start_now(sid: int, conn=Depends(get_conn),
                               _=Depends(api_user)):
        """Cut to this show immediately (stop the current song)."""
        ok, why = feeder.start_show_now(conn, sid, cut=True)
        if not ok:
            raise HTTPException(409, why)
        return {"ok": True, "detail": why}

    @app.post("/api/schedule/{sid}/cue_next")
    def api_schedule_cue_next(sid: int, conn=Depends(get_conn),
                              _=Depends(api_user)):
        """Play this show after the current song finishes (graceful)."""
        ok, why = feeder.start_show_now(conn, sid, cut=False)
        if not ok:
            raise HTTPException(409, why)
        return {"ok": True, "detail": why}

    @app.post("/api/schedule/stop_show")
    def api_schedule_stop_show(conn=Depends(get_conn), _=Depends(api_user)):
        ok, why = feeder.stop_show(conn)
        if not ok:
            raise HTTPException(409, why)
        return {"ok": True, "detail": why}

    # ------------------------------------------ spots (IDs / ads / jingles)

    @app.get("/api/spots/folders")
    def api_spot_folders(conn=Depends(get_conn), _=Depends(api_user)):
        """The configured station folders available for spot rules."""
        out = []
        for key, label, _hint in spotmod.FOLDER_CATEGORIES:
            path = coredb.get_setting(conn, key) or ""
            # 'ready' against the resolved path so a Z:\ folder still reads as
            # set up when this session has no Z: mapping
            out.append({"key": key, "label": label, "path": path,
                        "ready": bool(path) and os.path.isdir(pl.alias_path(path))})
        return out

    @app.get("/api/spots")
    def api_spots(conn=Depends(get_conn), _=Depends(api_user)):
        now = time.time()
        rules = []
        for r in spotmod.list_all(conn):
            rules.append({
                "id": r["id"], "folder_key": r["folder_key"],
                "file_path": r["file_path"],
                "folder_path": r.get("folder_path"),
                "pick_mode": r.get("pick_mode"),
                "label": r["label"], "trigger": r["trigger"],
                "enabled": bool(r["enabled"]),
                "end_date": r.get("end_date"),
                "summary": spotmod.describe(r),
                "next_epoch": spotmod.next_fire_epoch(r, now)})
        # soonest first; manual/disabled (next_epoch None) sink to the bottom
        rules.sort(key=lambda x: (x["next_epoch"] is None, x["next_epoch"] or 0))
        return {"now_epoch": now, "rules": rules}

    @app.post("/api/spots", status_code=201)
    def api_spots_add(body: dict, conn=Depends(get_conn), _=Depends(api_user)):
        trig = body.get("trigger")
        if trig not in spotmod.TRIGGERS:
            raise HTTPException(400, "trigger must be interval/clock/once/"
                                     "daily/weekly/manual")
        # target: a single file, a browsed folder (rotate/random), or a legacy
        # preset station folder (folder_key)
        file_path = (body.get("file_path") or "").strip() or None
        folder_path = (body.get("folder_path") or "").strip() or None
        pick_mode = "random" if body.get("pick_mode") == "random" else "rotate"
        key = body.get("folder_key") or ""
        if file_path:
            # validate resolved; store raw (a Z:\ pick that maps is valid)
            if not os.path.isfile(pl.alias_path(file_path)):
                raise HTTPException(400, "that file does not exist")
            key = ""
        elif folder_path:
            if not os.path.isdir(pl.alias_path(folder_path)):
                raise HTTPException(400, "that folder does not exist")
            key = ""
        elif key not in {k for k, _, _ in spotmod.FOLDER_CATEGORIES}:
            raise HTTPException(400, "pick a folder or a specific file")
        interval_min = clock_minutes = start_at = None
        time_of_day = days_mask = None
        if trig == "interval":
            try:
                interval_min = int(body.get("interval_min"))
            except (TypeError, ValueError):
                interval_min = 0
            if interval_min < 1:
                raise HTTPException(400, "minutes must be 1 or more")
        elif trig == "clock":
            clock_minutes = (body.get("clock_minutes") or "").strip()
            if not spotmod._parse_minutes(clock_minutes):
                raise HTTPException(400, "give minutes past the hour, e.g. 0 "
                                         "or 20,40")
        elif trig == "once":
            start_at = (body.get("start_at") or "").strip()
            if spotmod._parse_dt(start_at) is None:
                raise HTTPException(400, "start time must be YYYY-MM-DDTHH:MM")
        elif trig in ("daily", "weekly"):
            time_of_day = (body.get("time_of_day") or "").strip()
            try:
                _dt.datetime.strptime(time_of_day, "%H:%M")
            except ValueError:
                raise HTTPException(400, "time of day must be HH:MM")
            if trig == "weekly":
                try:
                    days_mask = int(body.get("days_mask") or 0)
                except (TypeError, ValueError):
                    days_mask = 0
                if not days_mask:
                    raise HTTPException(400, "pick at least one weekday")
        # optional run window (interval/clock/daily/weekly): the stop date
        start_date = (body.get("start_date") or "").strip() or None
        end_date = (body.get("end_date") or "").strip() or None
        for d in (start_date, end_date):
            if d is not None:
                try:
                    _dt.date.fromisoformat(d)
                except ValueError:
                    raise HTTPException(400, "dates must be YYYY-MM-DD")
        if start_date and end_date and end_date < start_date:
            raise HTTPException(400, "stop date is before the start date")
        rid = spotmod.add(conn, key, trig, interval_min, clock_minutes,
                          start_at, file_path=file_path, time_of_day=time_of_day,
                          days_mask=days_mask, start_date=start_date,
                          end_date=end_date,
                          folder_path=folder_path if not file_path else None,
                          pick_mode=pick_mode if folder_path and not file_path
                          else None)
        return {"id": rid}

    @app.delete("/api/spots/{rid}")
    def api_spots_remove(rid: int, conn=Depends(get_conn), _=Depends(api_user)):
        spotmod.remove(conn, rid)
        return {"ok": True}

    @app.post("/api/spots/{rid}/toggle")
    def api_spots_toggle(rid: int, conn=Depends(get_conn), _=Depends(api_user)):
        rule = spotmod.get(conn, rid)
        if rule is None:
            raise HTTPException(404, "spot rule not found")
        spotmod.set_enabled(conn, rid, not rule["enabled"])
        return {"ok": True, "enabled": not rule["enabled"]}

    @app.post("/api/spots/{rid}/play_now")
    def api_spots_play_now(rid: int, conn=Depends(get_conn), _=Depends(api_user)):
        """Drop this rule's spot in after the current song, right now."""
        rule = spotmod.get(conn, rid)
        if rule is None:
            raise HTTPException(404, "spot rule not found")
        ok, why = feeder.insert_spot(
            conn, rule["folder_key"], rule["label"],
            file_path=rule.get("file_path"),
            folder_path=rule.get("folder_path"),
            pick_mode=rule.get("pick_mode"))
        if not ok:
            raise HTTPException(409, why)
        return {"ok": True, "detail": why}

    @app.post("/api/engine/play_next")
    def api_play_next(body: PlayNextIn, conn=Depends(get_conn),
                      _=Depends(api_user)):
        """Cue a track immediately after the current song (§6 Phase 1)."""
        ok, why = feeder.insert_manual(conn, body.path, body.title)
        if not ok:
            code = 400 if why == "file could not be read/cached" else 502
            raise HTTPException(code, why)
        return {"ok": True, "title": why}

    # ------------------------------------------------- background loop
    stop = threading.Event()

    def loop():
        while not stop.wait(FEED_TICK_SEC):
            conn = coredb.connect(cfg["db_path"])
            try:
                feeder.tick(conn)
                feeder.fire_due_spots(conn)
                ingest_journal(conn, cfg["journal_path"])
            except Exception:
                log.exception("feeder tick failed")  # next tick tries again
            finally:
                conn.close()

    @app.on_event("startup")
    def start_loop():
        if cfg.get("feeder_enabled", True):
            threading.Thread(target=loop, name="feeder", daemon=True).start()

    @app.on_event("shutdown")
    def stop_loop():
        stop.set()
