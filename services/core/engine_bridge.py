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

DEFAULT_TRACK_SEC = 240.0   # estimate when a track's duration is unknown
FEED_TICK_SEC = 5.0
MAX_FEED_BATCH = 50         # sanity cap per tick


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

    def _load_manifest(self) -> dict:
        try:
            with open(self._manifest_path, "rb") as f:
                m = json.load(f)
            if isinstance(m.get("files"), dict):
                return m
        except (OSError, ValueError):
            pass
        return {"files": {}}

    def _save_manifest(self) -> None:
        tmp = self._manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._manifest, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._manifest_path)

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
        self._manifest["files"][dst] = {
            "src": src, "src_size": src_stat.st_size,
            "src_mtime": src_stat.st_mtime}
        self._save_manifest()
        return dst

    def evict_except(self, keep: set[str]) -> int:
        """Drop cached files not in keep (played/abandoned). Returns count."""
        victims = [p for p in self._manifest["files"] if p not in keep]
        for p in victims:
            try:
                os.remove(p)
            except OSError:
                pass
            del self._manifest["files"][p]
        if victims:
            self._save_manifest()
        return len(victims)


# ------------------------------------------------------------------- feeder

class Feeder:
    """Keeps P1's pending queue topped up from the active playlist."""

    def __init__(self, cfg: dict, engine: EngineClient, precache: Precache):
        self.cfg = cfg
        self.engine = engine
        self.precache = precache
        self.target_sec = cfg.get("precache_target_minutes", 45) * 60.0

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
    def _now_item_id(st: dict, now_id) -> int | None:
        """The base-rotation playlist_item id the play-head is currently on
        (None while a show/spot/emergency source is playing)."""
        if not now_id:
            return None
        for e in st["fed"]:
            if e["id"] == now_id and e.get("prog") == "base":
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
        show = st.get("show")
        return pl.get_items(conn, show["playlist_id"]) if show else []

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
        sched.set_state(conn, entry["id"], "playing")
        st["show"] = {"sched_id": entry["id"],
                      "playlist_id": entry["playlist_id"], "cursor": 0}
        log.warning("feeder: show '%s' on air (schedule %d) — interrupting "
                    "rotation at next boundary",
                    entry.get("playlist_name"), entry["id"])
        return self._clear_pending(conn, st, status)

    def _finish_show(self, conn, st: dict) -> None:
        show = st.get("show")
        if show:
            sched.set_state(conn, show["sched_id"], "done")
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
        if st.get("show"):
            return status  # one show at a time (MVP)
        entry = sched.due(conn)
        if entry is None:
            return status
        return self._start_show(conn, st, entry, status)

    def start_show_now(self, conn, sched_id: int) -> tuple[bool, str]:
        """Manual cue: put a waiting show on air right now (next boundary)."""
        status = self.engine.status()
        if status is None:
            return False, "engine unreachable"
        entry = sched.get(conn, sched_id)
        if entry is None or entry["state"] != "waiting":
            return False, "that show is not waiting to start"
        st = self._load_state(conn)
        if st.get("show"):
            return False, "a show is already on air"
        if "pending_ids" in status:  # keep bookkeeping sane before we clear
            live = set(status["pending_ids"])
            st["fed"] = [e for e in st["fed"] if e["id"] in live]
        self._start_show(conn, st, entry, status)
        self._save_state(conn, st)
        ok, why = self.tick(conn)
        return ok, f"show started ({why})"

    def resync_rotation(self, conn) -> tuple[bool, str]:
        """After a permanent edit to the active rotation playlist, rebuild the
        pre-cached buffer so the change takes effect right away. The current
        song keeps playing; everything after it is re-fed from the edited list
        starting just after wherever the play-head is."""
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
        cur_item_id = (cur.get("pl_item_id") if cur and cur.get("prog") == "base"
                       else st.get("now_item_id"))
        idx_by_id = {it["id"]: i for i, it in enumerate(items)}
        if cur_item_id in idx_by_id:            # resume right after the play-head
            st["cursor"] = idx_by_id[cur_item_id] + 1
        else:                                   # play-head item gone/unknown
            st["cursor"] = min(st.get("cursor", 0), len(items))
        # while a show is on air the base buffer isn't live — just fix the
        # cursor so the edit takes effect when the rotation resumes
        if st.get("show"):
            self._save_state(conn, st)
            return True, "rotation cursor updated (show on air)"
        # drop the buffer (current song keeps playing) and re-feed the new order
        status = self._clear_pending(conn, st, status)
        if cur is not None:
            st["fed"] = [cur]  # keep the play-head so the now-marker survives
        self._save_state(conn, st)
        ok, why = self.tick(conn)
        return ok, f"re-synced ({why})"

    # ---- spots: station IDs / ads / jingles / PSAs between songs (§ spots)

    def insert_spot(self, conn, folder_key: str,
                    label: str | None = None) -> tuple[bool, str]:
        """Resolve one round-robin file from a settings folder and drop it in
        right after the current song (airs at the next boundary)."""
        status = self.engine.status()
        if status is None:
            return False, "engine unreachable"
        folder = coredb.get_setting(conn, folder_key)
        if not folder or not os.path.isdir(folder):
            return False, f"the {label or folder_key} folder is not set up"
        src = pl.resolve_item(conn, {"item_type": "folder-rotation",
                                     "path": folder})
        if src is None:
            return False, "no playable files in that folder"
        cached = self.precache.ensure(src)
        if cached is None:
            return False, "the spot file could not be cached"
        title = f"{label or spotmod.default_label(folder_key)}: " + \
            os.path.splitext(os.path.basename(src))[0]
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
                             "title": title, "prog": "spot"})
        self._save_state(conn, st)
        return True, title

    def fire_due_spots(self, conn) -> None:
        """Called every tick: fire any spot rule that is due (all trigger
        types except manual). Spots play everywhere, shows included."""
        status = self.engine.status()
        if status is None or not status.get("now_playing") \
                or status.get("emergency_mode"):
            return  # nothing airing / in failover — don't inject
        now = time.time()
        for rule in spotmod.list_enabled(conn):
            if rule["trigger"] == "manual" or not spotmod.due(rule, now):
                continue
            ok, why = self.insert_spot(conn, rule["folder_key"], rule["label"])
            spotmod.mark_fired(conn, rule, now)  # advance schedule either way
            if ok:
                log.info("spot fired (%s): %s", rule["folder_key"], why)
            else:
                log.warning("spot rule %d skipped: %s", rule["id"], why)

    def tick(self, conn, op: str = "append") -> tuple[bool, str]:
        status = self.engine.status()
        if status is None:
            return False, "engine unreachable"

        st = self._load_state(conn)
        # reconcile: keep entries P1 still has pending, PLUS the one currently
        # playing (so we can map the play-head back to a playlist item for the
        # rotation view's "now" marker). Count fallback if no pending_ids.
        now_id = status.get("now_id")
        if op == "replace":
            st["fed"] = []
        elif "pending_ids" in status:
            keep = set(status["pending_ids"])
            if now_id:
                keep.add(now_id)
            st["fed"] = [e for e in st["fed"] if e["id"] in keep]
        else:
            pending_count = max(0, status["queue_len"]
                                - status["current_index"] - 1)
            if len(st["fed"]) > pending_count:
                st["fed"] = st["fed"][len(st["fed"]) - pending_count:]
        st["now_item_id"] = self._now_item_id(st, now_id)

        # a show that has fully aired hands back to the rotation
        self._finalize_show_if_aired(conn, st, status)
        # a scheduled show whose time has come interrupts the rotation
        if op != "replace":
            status = self._maybe_fire_scheduled(conn, st, status)

        base_pid = coredb.get_setting(conn, "active_playlist_id")
        base_items = pl.get_items(conn, int(base_pid)) if base_pid else []
        if not base_items and not st.get("show"):
            self._save_state(conn, st)
            return True, "no active playlist"

        # the currently-playing entry is kept in fed for the now-marker but is
        # not "pending work" — don't count it toward the top-up target
        pending = [e for e in st["fed"] if e["id"] != now_id]
        pending_sec = sum(e["duration"] for e in pending)
        if pending and pending_sec >= self.target_sec:
            self._save_state(conn, st)
            self._evict(conn, st, status)
            return True, "topped up"

        # build a batch up to the duration target: the active show plays once
        # through first, then the base rotation carries on forever
        show_items = self._show_items(conn, st)
        batch = []
        cache_fails = 0
        while pending_sec < self.target_sec and len(batch) < MAX_FEED_BATCH:
            show = st.get("show")
            if show and not show.get("done_feeding"):
                resolved = self._next_resolved(conn, show_items, show,
                                               wrap=False)
                if resolved is None:      # fully fed -> fill the rest with base
                    show["done_feeding"] = True
                    show_items = []
                    continue
                prog, source = "show", "show"
            else:
                if not base_items:
                    break
                resolved = self._next_resolved(conn, base_items, st, wrap=True)
                if resolved is None:
                    break
                prog, source = "base", "playlist"
            src, title, item_id = resolved
            cached = self.precache.ensure(src)
            if cached is None:
                cache_fails += 1
                if cache_fails >= max(1, len(base_items) + len(show_items)):
                    break  # NAS is gone — stop burning the tick, retry later
                continue  # source vanished mid-feed; try the next item
            duration = self._duration_of(conn, src)
            eid = uuid.uuid4().hex
            batch.append({"id": eid, "path": cached, "title": title,
                          "source": source, "src": src})
            st["fed"].append({"id": eid, "path": cached, "duration": duration,
                              "title": title, "prog": prog,
                              # only base-rotation items mark the rotation view
                              "pl_item_id": item_id if prog == "base" else None})
            pending_sec += duration
        if not batch and op != "replace":
            self._save_state(conn, st)
            return True, "nothing to feed"

        mutation = {"op": op, "queue_version": status["queue_version"] + 1,
                    "entries": batch}
        code, body = self.engine.queue(mutation)
        if code == 409:  # someone else bumped the version — re-sync, retry
            fresh = body.get("status") or self.engine.status() or {}
            mutation["queue_version"] = fresh.get("queue_version", 0) + 1
            code, body = self.engine.queue(mutation)
        if code != 202:
            # roll back bookkeeping for the rejected batch
            fed_ids = {b["id"] for b in batch}
            st["fed"] = [e for e in st["fed"] if e["id"] not in fed_ids]
            self._save_state(conn, st)
            return False, f"queue push failed ({code}): {body}"
        st["queue_version"] = mutation["queue_version"]
        self._save_state(conn, st)
        self._evict(conn, st, status)
        return True, f"fed {len(batch)} entries"

    def _evict(self, conn, st: dict, status: dict) -> None:
        keep = {e["path"] for e in st["fed"]}
        now_playing = status.get("now_playing")
        if now_playing:
            keep.add(now_playing)
        n = self.precache.evict_except(keep)
        if n:
            log.info("precache: evicted %d played file(s)", n)


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
        fst = feeder._load_state(conn)
        pending = fst["fed"]
        if st is not None and "pending_ids" in st:
            order = {i: k for k, i in enumerate(st["pending_ids"])}
            pending = sorted((e for e in pending if e["id"] in order),
                             key=lambda e: order[e["id"]])
        elif st is not None:
            n = max(0, st["queue_len"] - st["current_index"] - 1)
            if len(pending) > n:
                pending = pending[len(pending) - n:]
        return {"engine_online": st is not None,
                "now_playing": (st or {}).get("now_playing"),
                "now_title": (st or {}).get("now_title"),
                "now_source": (st or {}).get("now_source"),
                "duration": (st or {}).get("duration"),
                "position": (st or {}).get("position"),
                "paused": (st or {}).get("paused", False),
                "stop_after_current": (st or {}).get("stop_after_current", False),
                "emergency_mode": (st or {}).get("emergency_mode", False),
                "forced_emergency": (st or {}).get("forced_emergency", False),
                "pending": [{"id": e["id"],
                             "title": e.get("title") or "(untitled)",
                             "duration": e.get("duration")}
                            for e in pending]}

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
        """The active rotation playlist in full + which item is on air now."""
        base_pid = coredb.get_setting(conn, "active_playlist_id")
        row = conn.execute("SELECT id, name FROM playlists WHERE id = ?",
                           (int(base_pid),)).fetchone() if base_pid else None
        if row is None:
            return {"playlist": None, "items": [], "now_item_id": None}
        items = pl.get_items(conn, row["id"])
        st = feeder._load_state(conn)
        now_item_id = feeder._now_item_id(st, (engine.status() or {}).get("now_id"))
        return {
            "playlist": {"id": row["id"], "name": row["name"]},
            "now_item_id": now_item_id,
            "items": [{"id": it["id"], "item_type": it["item_type"],
                       "title": it["title"] or os.path.splitext(
                           os.path.basename(it["path"]))[0]}
                      for it in items],
        }

    @app.get("/api/history")
    def api_history(limit: int = 40, conn=Depends(get_conn),
                    _=Depends(api_user)):
        """As-aired log: the most recent track starts/ends from play_history."""
        rows = conn.execute(
            "SELECT ts, event, title, source, path FROM play_history "
            "WHERE event IN ('track_start', 'track_end') "
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
        pid = _active_pid(conn)
        item_ids = body.get("item_ids")
        if not isinstance(item_ids, list):
            raise HTTPException(400, "item_ids must be a list")
        existing = {i["id"] for i in pl.get_items(conn, pid)}
        if set(item_ids) != existing:
            raise HTTPException(409, "list changed — reload the page")
        pl.reorder_items(conn, pid, item_ids)     # permanent edit
        feeder.resync_rotation(conn)              # take effect on air now
        return {"ok": True}

    @app.post("/api/rotation/remove")
    def api_rotation_remove(body: dict, conn=Depends(get_conn),
                            _=Depends(api_user)):
        pid = _active_pid(conn)
        try:
            item_id = int(body.get("item_id"))
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
            "current_show": ({"sched_id": cur["id"],
                              "name": cur["playlist_name"]} if cur else None),
            "now": sched.now_local(),
            "upcoming": [{"id": e["id"], "playlist_id": e["playlist_id"],
                          "name": e["playlist_name"], "start_at": e["start_at"]}
                         for e in sched.list_waiting(conn)],
        }

    @app.post("/api/schedule", status_code=201)
    def api_schedule_add(body: dict, conn=Depends(get_conn),
                         _=Depends(api_user)):
        try:
            pid = int(body.get("playlist_id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "playlist_id required")
        if conn.execute("SELECT 1 FROM playlists WHERE id = ?",
                        (pid,)).fetchone() is None:
            raise HTTPException(404, "playlist not found")
        start_at = (body.get("start_at") or "").strip() or None
        if start_at and len(start_at) < 16:  # 'YYYY-MM-DDTHH:MM'
            raise HTTPException(400, "start time must be YYYY-MM-DDTHH:MM")
        return {"id": sched.add(conn, pid, start_at)}

    @app.delete("/api/schedule/{sid}")
    def api_schedule_remove(sid: int, conn=Depends(get_conn),
                            _=Depends(api_user)):
        sched.remove(conn, sid)
        return {"ok": True}

    @app.post("/api/schedule/{sid}/start_now")
    def api_schedule_start_now(sid: int, conn=Depends(get_conn),
                               _=Depends(api_user)):
        ok, why = feeder.start_show_now(conn, sid)
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
            out.append({"key": key, "label": label, "path": path,
                        "ready": bool(path) and os.path.isdir(path)})
        return out

    @app.get("/api/spots")
    def api_spots(conn=Depends(get_conn), _=Depends(api_user)):
        now = time.time()
        rules = []
        for r in spotmod.list_all(conn):
            rules.append({
                "id": r["id"], "folder_key": r["folder_key"],
                "label": r["label"], "trigger": r["trigger"],
                "enabled": bool(r["enabled"]),
                "summary": spotmod.describe(r),
                "next_epoch": spotmod.next_fire_epoch(r, now)})
        # soonest first; manual/disabled (next_epoch None) sink to the bottom
        rules.sort(key=lambda x: (x["next_epoch"] is None, x["next_epoch"] or 0))
        return {"now_epoch": now, "rules": rules}

    @app.post("/api/spots", status_code=201)
    def api_spots_add(body: dict, conn=Depends(get_conn), _=Depends(api_user)):
        key = body.get("folder_key")
        if key not in {k for k, _, _ in spotmod.FOLDER_CATEGORIES}:
            raise HTTPException(400, "unknown folder")
        trig = body.get("trigger")
        if trig not in spotmod.TRIGGERS:
            raise HTTPException(400, "trigger must be interval/clock/once/manual")
        interval_min = clock_minutes = start_at = None
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
        rid = spotmod.add(conn, key, trig, interval_min, clock_minutes, start_at)
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
        ok, why = feeder.insert_spot(conn, rule["folder_key"], rule["label"])
        if not ok:
            raise HTTPException(409, why)
        return {"ok": True, "detail": why}

    @app.post("/api/engine/play_next")
    def api_play_next(body: PlayNextIn, conn=Depends(get_conn),
                      _=Depends(api_user)):
        """Cue a track immediately after the current song (§6 Phase 1)."""
        status = engine.status()
        if status is None:
            raise HTTPException(502, "engine unreachable")
        cached = precache.ensure(body.path)
        if cached is None:
            raise HTTPException(400, "file could not be read/cached")
        title = body.title or os.path.splitext(
            os.path.basename(body.path))[0]
        entry = {"id": uuid.uuid4().hex, "path": cached, "title": title,
                 "source": "manual", "src": body.path}
        mutation = {"op": "insert_next",
                    "queue_version": status["queue_version"] + 1,
                    "entries": [entry]}
        code, resp = engine.queue(mutation)
        if code == 409:
            fresh = resp.get("status") or engine.status() or {}
            mutation["queue_version"] = fresh.get("queue_version", 0) + 1
            code, resp = engine.queue(mutation)
        if code != 202:
            raise HTTPException(502, f"engine said {code}: {resp}")
        # tell the feeder so queue view + eviction know about it
        st = feeder._load_state(conn)
        st["fed"].insert(0, {"id": entry["id"], "path": cached,
                             "duration": feeder._duration_of(conn, body.path),
                             "title": title})
        feeder._save_state(conn, st)
        return {"ok": True, "title": title}

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
