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
import uuid

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from . import db as coredb
from . import playlists as pl

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
        st.setdefault("fed", [])            # [{id, path, duration}] pending
        st.setdefault("queue_version", 0)   # last version we know of
        st.setdefault("cursor", 0)          # position in active playlist
        return st

    def _save_state(self, conn, st: dict) -> None:
        coredb.set_setting(conn, "feeder_state", json.dumps(st))

    def _duration_of(self, conn, src: str) -> float:
        row = conn.execute("SELECT duration_sec FROM tracks WHERE path = ?",
                           (src,)).fetchone()
        if row and row["duration_sec"]:
            return float(row["duration_sec"])
        return DEFAULT_TRACK_SEC

    def _next_resolved(self, conn, items: list[dict], st: dict):
        """Resolve the next playable playlist item; advances the cursor.
        Wraps forever. Returns (src, title) or None if a full lap resolved
        nothing (all sources empty/missing)."""
        for _ in range(len(items)):
            item = items[st["cursor"] % len(items)]
            st["cursor"] = (st["cursor"] + 1) % len(items)
            src = pl.resolve_item(conn, item)
            if src is not None:
                return src, item.get("title") or os.path.splitext(
                    os.path.basename(src))[0]
            log.warning("feeder: item unresolvable (skip+alert, §10.5): %r",
                        item["path"])
        return None

    def activate(self, conn, playlist_id: int) -> tuple[bool, str]:
        """Make a playlist the live rotation: replace P1's queue now."""
        coredb.set_setting(conn, "active_playlist_id", str(playlist_id))
        st = self._load_state(conn)
        st["fed"], st["cursor"] = [], 0
        self._save_state(conn, st)
        ok, why = self.tick(conn, op="replace")
        return ok, why

    def tick(self, conn, op: str = "append") -> tuple[bool, str]:
        status = self.engine.status()
        if status is None:
            return False, "engine unreachable"
        pid_raw = coredb.get_setting(conn, "active_playlist_id")
        if not pid_raw:
            return True, "no active playlist"
        items = pl.get_items(conn, int(pid_raw))
        if not items:
            return True, "active playlist is empty"

        st = self._load_state(conn)
        # reconcile: drop bookkeeping for entries P1 has already played,
        # by identity (P1 publishes pending ids) with a count fallback
        if op == "replace":
            st["fed"] = []
        elif "pending_ids" in status:
            live = set(status["pending_ids"])
            st["fed"] = [e for e in st["fed"] if e["id"] in live]
        else:
            pending_count = max(0, status["queue_len"]
                                - status["current_index"] - 1)
            if len(st["fed"]) > pending_count:
                st["fed"] = st["fed"][len(st["fed"]) - pending_count:]
        pending_sec = sum(e["duration"] for e in st["fed"])
        if st["fed"] and pending_sec >= self.target_sec:
            self._save_state(conn, st)
            self._evict(conn, st, status)
            return True, "topped up"

        # build a batch up to the duration target
        batch = []
        cache_fails = 0
        while pending_sec < self.target_sec and len(batch) < MAX_FEED_BATCH:
            resolved = self._next_resolved(conn, items, st)
            if resolved is None:
                break
            src, title = resolved
            cached = self.precache.ensure(src)
            if cached is None:
                cache_fails += 1
                if cache_fails >= len(items):
                    break  # NAS is gone — stop burning the tick, retry later
                continue  # source vanished mid-feed; try the next item
            duration = self._duration_of(conn, src)
            batch.append({"id": uuid.uuid4().hex, "path": cached,
                          "title": title, "source": "playlist", "src": src})
            st["fed"].append({"id": batch[-1]["id"], "path": cached,
                              "duration": duration, "title": title})
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
        if op not in ("pause", "resume", "skip"):
            raise HTTPException(400, "op must be pause/resume/skip")
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
                "position": (st or {}).get("position"),
                "paused": (st or {}).get("paused", False),
                "emergency_mode": (st or {}).get("emergency_mode", False),
                "pending": [{"title": e.get("title") or "(untitled)",
                             "duration": e.get("duration")}
                            for e in pending]}

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
