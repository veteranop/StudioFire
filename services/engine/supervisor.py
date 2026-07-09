"""P1 engine supervisor: watchdog, failover chain, track advance.

Binding spec: PLAN.md §10.1/§10.2. P1 design laws: STDLIB ONLY.

Threading model (keeps §10.2's single-writer guarantee):
- OWNER THREAD: the only thread that touches QueueState, talks playback
  policy to mpv, and calls QueueStore.save(). It drains an action queue.
- WATCHDOG THREAD: every ~1s checks mpv liveness (ping) + position advance;
  posts actions to the owner thread; writes the heartbeat file. Never
  mutates state itself.
- mpv I/O thread (inside MpvClient) posts mpv events as actions.

Failover chain (single rule, §10.1): any condition where the next track
cannot start -> emergency folder loop -> cached music (precache dir,
scanned fresh — its contents churn) -> baked-in last-resort source.
Exit emergency as soon as a playable queue item exists again.

The emergency folder is optional: stations that ship no filler assets get
real rotation music from the precache as their emergency audio instead.

The baked-in tier defaults to an ffmpeg-generated tone (av://lavfi) so it
exists even if every file on disk is gone; a real station-ID file can be
configured instead (config key engine.baked_in_asset).
"""

from __future__ import annotations

import logging
import os
import queue as queue_mod
import subprocess
import threading
import time

from .journal import Journal
from .mpv_ipc import MpvClient, MpvDead, MpvError
from .queue_store import QueueStore, QueueState, apply_mutation

log = logging.getLogger("engine.supervisor")

BAKED_IN_DEFAULT = "av://lavfi:sine=frequency=600:sample_rate=48000"
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
              ".wma", ".aiff", ".aif"}
STALL_TICKS = 2          # position frozen for N watchdog ticks -> restart mpv
RESTART_BACKOFF = 1.0    # seconds between consecutive mpv restarts
MAX_QUEUE_HISTORY = 20   # played entries kept in the runtime queue (journal is
                         # the permanent record); older ones are trimmed


def playable(path: str) -> bool:
    if path.startswith("av://"):
        return True
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def probe_decodable(mpv_path: str, media_path: str, timeout: float = 10.0) -> bool:
    """Startup validation for emergency-folder files (§10.1): decode a
    slice with a throwaway mpv on the null audio output."""
    try:
        rc = subprocess.run(
            [mpv_path, "--no-config", "--no-video", "--no-terminal",
             "--ao=null", "--end=0.5", media_path],
            timeout=timeout,
            # DEVNULL all stdio: mpv hangs if it inherits console handles
            # under CREATE_NO_WINDOW (bench-bisected; do not remove)
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).returncode
        return rc == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


class EngineSupervisor:
    def __init__(self, config: dict):
        c = dict(config)
        self._mpv_path = c["mpv_path"]
        self._pipe_name = c.get("pipe_name", "studiofire-engine")
        self._audio_device = c.get("audio_device") or None
        self._emergency_dir = c["emergency_dir"]
        self._precache_dir = c.get("precache_dir") or ""
        self._baked_in = c.get("baked_in_asset") or BAKED_IN_DEFAULT
        self._extra_mpv_args = list(c.get("extra_mpv_args", []))
        self._watchdog_interval = float(c.get("watchdog_interval", 1.0))
        self._heartbeat_path = c["heartbeat_path"]

        self._store = QueueStore(c["state_path"])
        self._journal = Journal(c["journal_path"])
        self._state: QueueState = QueueState()

        self._actions: queue_mod.Queue = queue_mod.Queue()
        self._client: MpvClient | None = None
        self._owner: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._stopping = threading.Event()

        self._emergency_files: list[str] = []
        self._emergency_idx = 0
        self._expected_next_path: str | None = None  # appended to mpv for prefetch
        self._stop_after_current = False  # pause when the current song ends
        self._last_restart = 0.0

        self._status_lock = threading.Lock()
        self._status = {"now_playing": None, "now_title": None,
                        "now_source": None, "now_id": None, "duration": None,
                        "position": None, "paused": False,
                        "stop_after_current": False,
                        "emergency_mode": False, "forced_emergency": False,
                        "mpv_alive": False,
                        "queue_version": 0, "current_index": -1,
                        "queue_len": 0, "pending_ids": []}

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._journal.append("engine_start")
        self._state = self._store.load()
        # publish the restored state so /status is truthful before playback
        self._set_status(queue_version=self._state.queue_version,
                         current_index=self._state.current_index,
                         queue_len=len(self._state.entries),
                         pending_ids=self._pending_ids(),
                         emergency_mode=self._state.emergency_mode,
                         forced_emergency=self._state.forced_emergency)
        self._validate_emergency_folder()
        self._start_mpv()
        self._owner = threading.Thread(target=self._owner_loop,
                                       name="engine-owner", daemon=True)
        self._owner.start()
        self._watchdog = threading.Thread(target=self._watchdog_loop,
                                          name="engine-watchdog", daemon=True)
        self._watchdog.start()
        # resume where we left off (or re-enter emergency per persisted flag)
        self._post({"kind": "kick", "why": "startup"})

    def stop(self) -> None:
        self._stopping.set()
        self._post({"kind": "shutdown"})
        if self._owner:
            self._owner.join(5)
        if self._watchdog:
            self._watchdog.join(2)
        if self._client:
            self._client.stop()
        self._journal.append("engine_stop")
        self._journal.close()

    # ------------------------------------------------------------ public API

    def submit_mutation(self, mutation: dict, timeout: float = 3.0) -> tuple[bool, str]:
        """Thread-safe entry point for P2 queue ops (via control.py)."""
        done = threading.Event()
        result: list = [False, "engine shutting down"]
        self._post({"kind": "mutation", "mutation": mutation,
                    "done": done, "result": result})
        done.wait(timeout)
        return result[0], result[1]

    def submit_command(self, op: str, timeout: float = 3.0) -> tuple[bool, str]:
        """skip / pause / resume."""
        done = threading.Event()
        result: list = [False, "engine shutting down"]
        self._post({"kind": "op", "op": op, "done": done, "result": result})
        done.wait(timeout)
        return result[0], result[1]

    def status(self) -> dict:
        with self._status_lock:
            return dict(self._status)

    # ---------------------------------------------------------- owner thread

    def _post(self, action: dict) -> None:
        self._actions.put(action)

    def _owner_loop(self) -> None:
        while True:
            action = self._actions.get()
            kind = action.get("kind")
            try:
                if kind == "shutdown":
                    return
                elif kind == "mpv_event":
                    self._handle_mpv_event(action["msg"])
                elif kind == "mutation":
                    ok, why = self._handle_mutation(action["mutation"])
                    action["result"][:] = [ok, why]
                    action["done"].set()
                elif kind == "op":
                    ok, why = self._handle_op(action["op"])
                    action["result"][:] = [ok, why]
                    action["done"].set()
                elif kind == "mpv_stalled":
                    self._restart_mpv(action.get("why", "watchdog"))
                elif kind == "kick":
                    self._kick(action.get("why", ""))
            except MpvDead as exc:
                log.error("mpv died during '%s': %s", kind, exc)
                self._restart_mpv(f"MpvDead during {kind}")
            except Exception:
                log.exception("owner loop error handling %s", kind)
                # Never let a logic bug kill the loop; failover keeps air alive.
                self._safe_enter_emergency("owner loop exception")

    # ----------------------------------------------------------- mpv events

    def _on_mpv_event(self, msg: dict) -> None:  # runs on mpv I/O thread
        if msg.get("event") in ("start-file", "end-file", "idle"):
            self._post({"kind": "mpv_event", "msg": msg})

    def _handle_mpv_event(self, msg: dict) -> None:
        event = msg.get("event")
        if event == "start-file":
            self._on_start_file()
        elif event == "end-file":
            self._on_end_file(msg.get("reason", "unknown"),
                              msg.get("file_error"))
        elif event == "idle":
            # mpv has nothing to play — this must never be silent air
            if not self._stopping.is_set():
                self._advance_or_fail("mpv idle")

    def _on_start_file(self) -> None:
        path = self._try_get("path")
        if path is None:
            return
        entry = self._match_queue_entry(path)
        if entry is not None:
            idx, e = entry
            changed = idx != self._state.current_index
            if changed:
                self._state.current_index = idx
            # bound runtime memory: keep only the last N played entries. `e` is
            # already captured, and current + pending are untouched, so the
            # advance/prefetch below stay correct.
            if self._state.trim_history(MAX_QUEUE_HISTORY):
                changed = True
            if changed:
                self._store.save(self._state)
            if self._state.emergency_mode:
                self._exit_emergency(reason="queue track started")
            self._journal.append("track_start", path=path,
                                 title=e.get("title"), source=e.get("source"))
            # status must reflect the advance, not just mutations — P2's
            # feeder computes pending work from these fields
            self._set_status(current_index=self._state.current_index,
                             queue_len=len(self._state.entries),
                             pending_ids=self._pending_ids())
            self._ensure_next_appended()
            self._set_status(now_title=e.get("title"),
                             now_source=e.get("source"), now_id=e.get("id"))
        else:
            source = "emergency" if path != self._baked_in else "baked_in"
            self._journal.append("track_start", path=path, source=source)
            self._set_status(now_title=None, now_source=source, now_id=None)
        self._set_status(now_playing=path, duration=None)
        # "Stop after current song": the previous song has ended and this one
        # just loaded — hold it at the start until the operator goes on air.
        if self._stop_after_current:
            self._stop_after_current = False
            self._journal.append("stopped_after_song", path=path)
            log.warning("stopped on air after operator's stop-after request")
            try:
                self._client.set_property("pause", True)
            except (MpvDead, MpvError):
                pass
            self._set_status(stop_after_current=False, paused=True)

    def _on_end_file(self, reason: str, file_error) -> None:
        path = self._status_get("now_playing")
        self._journal.append("track_end", path=path, reason=reason,
                             error=file_error)
        if reason == "error":
            log.error("decode/play error on %s: %s", path, file_error)
        # eof/error with a prefetched next -> mpv auto-advances and start-file
        # will arrive. If nothing follows, the 'idle' event triggers failover.

    # ------------------------------------------------------- queue mechanics

    def _match_queue_entry(self, path: str):
        norm = os.path.normcase(os.path.normpath(path))
        # search near current index first (advance is the common case)
        order = [self._state.current_index, self._state.current_index + 1]
        order += range(len(self._state.entries))
        seen = set()
        for i in order:
            if i in seen or not (0 <= i < len(self._state.entries)):
                continue
            seen.add(i)
            e = self._state.entries[i]
            if os.path.normcase(os.path.normpath(e["path"])) == norm:
                return i, e
        return None

    def _ensure_next_appended(self) -> None:
        """Keep mpv's playlist primed with the next playable queue item."""
        nxt = self._state.next_entry()
        if nxt is None:
            self._expected_next_path = None
            return
        if not playable(nxt["path"]):
            self._journal.append("track_skip", path=nxt["path"],
                                 reason="unplayable at prefetch")
            # drop it from ahead-of-us so mpv never sees it
            del self._state.entries[self._state.current_index + 1]
            self._store.save(self._state)
            self._set_status(queue_len=len(self._state.entries),
                             pending_ids=self._pending_ids())
            return self._ensure_next_appended()
        if self._expected_next_path == nxt["path"]:
            return  # already primed
        # Reduce mpv's playlist to just the currently-playing file, then append
        # the one true next. mpv KEEPS finished files in its playlist, so after
        # an eof-advance the current track sits at playlist-pos > 0 with the
        # played file(s) before it. The old code blindly removed the LAST entry
        # ("count-1"), which after an advance is the CURRENT track — mpv stopped
        # it (end-file reason 'stop') and fell through to the append, silently
        # skipping ~every other queued item (spots included). Remove every entry
        # except the current one, and never the current itself.
        pos = self._try_get("playlist-pos")
        count = self._try_get("playlist-count")
        if isinstance(pos, int) and isinstance(count, int) and count > 0:
            for i in range(count - 1, pos, -1):    # stale primes AFTER current
                self._client.command("playlist-remove", i)
            for i in range(pos - 1, -1, -1):        # played files BEFORE current
                self._client.command("playlist-remove", i)
        self._client.command("loadfile", nxt["path"], "append")
        self._expected_next_path = nxt["path"]

    def _advance_or_fail(self, why: str) -> None:
        """mpv is idle. Start the next playable thing NOW (§10.1)."""
        if self._state.emergency_mode:
            self._play_next_emergency()
            return
        # find next playable queue entry
        idx = self._state.current_index + 1
        while idx < len(self._state.entries):
            e = self._state.entries[idx]
            if playable(e["path"]):
                self._state.current_index = idx - 1  # start-file will bump it
                self._store.save(self._state)
                self._expected_next_path = None
                self._client.command("loadfile", e["path"], "replace")
                return
            self._journal.append("track_skip", path=e["path"],
                                 reason="unplayable at advance")
            idx += 1
        self._enter_emergency(why)

    def _kick(self, why: str) -> None:
        """(Re)start playback according to persisted state, e.g. at startup."""
        if self._state.emergency_mode:
            self._enter_emergency(f"persisted emergency_mode ({why})")
        else:
            self._advance_or_fail(f"kick: {why}")

    # -------------------------------------------------------------- failover

    def _validate_emergency_folder(self) -> None:
        files = []
        try:
            names = sorted(os.listdir(self._emergency_dir))
        except OSError:
            names = []
        for n in names:
            p = os.path.join(self._emergency_dir, n)
            if not os.path.isfile(p):
                continue
            if os.path.splitext(n)[1].lower() not in AUDIO_EXTS:
                continue  # .gitkeep and friends are not bad assets
            if probe_decodable(self._mpv_path, p):
                files.append(p)
            else:
                self._journal.append("emergency_asset_bad", path=p)
                log.error("emergency folder file failed decode probe: %s", p)
        self._emergency_files = files
        if not files:
            self._journal.append("emergency_folder_empty",
                                 dir=self._emergency_dir)
            log.warning("emergency folder empty/invalid (%s) — cached music "
                        "(%s) is the filler tier; baked-in source is the "
                        "last resort", self._emergency_dir,
                        self._precache_dir or "no precache dir configured")

    def _emergency_candidates(self) -> list[str]:
        """Playable filler, best tier first: curated emergency-folder assets,
        else real music from the precache dir (scanned fresh each time — the
        feeder adds/evicts files constantly, so the startup snapshot model
        used for the emergency folder doesn't apply)."""
        files = [p for p in self._emergency_files if playable(p)]
        if files:
            return files
        if self._precache_dir:
            try:
                names = sorted(os.listdir(self._precache_dir))
            except OSError:
                names = []
            for n in names:
                if os.path.splitext(n)[1].lower() not in AUDIO_EXTS:
                    continue
                p = os.path.join(self._precache_dir, n)
                if playable(p):
                    files.append(p)
        return files

    def _enter_emergency(self, why: str) -> None:
        if not self._state.emergency_mode:
            self._journal.append("emergency_enter", reason=why)
            log.error("ENTERING EMERGENCY MODE: %s", why)
            self._state.emergency_mode = True
            self._store.save(self._state)
        self._set_status(emergency_mode=True)
        self._expected_next_path = None
        self._play_next_emergency()

    def _play_next_emergency(self) -> None:
        # first, has P2 given us something playable in the meantime?
        # (unless the operator FORCED emergency — then stay on filler
        # until an explicit resume_normal)
        if not self._state.forced_emergency:
            nxt = self._state.next_entry()
            if nxt is not None and playable(nxt["path"]):
                self._client.command("loadfile", nxt["path"], "replace")
                return  # start-file handler will exit emergency mode
        candidates = self._emergency_candidates()
        if candidates:
            p = candidates[self._emergency_idx % len(candidates)]
            self._emergency_idx += 1
            self._client.command("loadfile", p, "replace")
        else:
            # tier 3: baked-in source, looped — the last line of defense
            self._client.command("loadfile", self._baked_in, "replace")
            self._client.set_property("loop-file", "inf")

    def _exit_emergency(self, reason: str) -> None:
        self._state.emergency_mode = False
        self._state.forced_emergency = False
        self._store.save(self._state)
        self._set_status(emergency_mode=False, forced_emergency=False)
        try:
            self._client.set_property("loop-file", "no")
        except (MpvDead, MpvError):
            pass
        self._journal.append("emergency_exit", reason=reason)
        log.warning("exited emergency mode: %s", reason)

    def _safe_enter_emergency(self, why: str) -> None:
        try:
            self._enter_emergency(why)
        except Exception:
            log.exception("failover itself failed; restarting mpv")
            try:
                self._restart_mpv("failover failure")
            except Exception:
                log.exception("mpv restart also failed — will retry on watchdog")

    # ------------------------------------------------------------- mutations

    def _handle_mutation(self, mutation: dict) -> tuple[bool, str]:
        ok, why = apply_mutation(self._state, mutation)
        if ok:
            self._store.save(self._state)
            self._set_status(queue_version=self._state.queue_version,
                             current_index=self._state.current_index,
                             queue_len=len(self._state.entries),
                             pending_ids=self._pending_ids())
            self._expected_next_path = None  # re-evaluate prefetch
            if self._state.forced_emergency:
                pass  # queued for later; operator holds us on filler
            elif mutation.get("op") == "replace":
                self._advance_or_fail("queue replaced")
            elif self._state.emergency_mode:
                self._play_next_emergency()  # new material may end emergency
            else:
                idle = bool(self._try_get("idle-active"))
                if idle:
                    self._advance_or_fail("mutation while idle")
                else:
                    self._ensure_next_appended()
        return ok, why

    def _handle_op(self, op: str) -> tuple[bool, str]:
        if op == "skip":
            self._advance_or_fail("operator skip")
            return True, "ok"
        if op == "pause":
            self._client.set_property("pause", True)
            self._set_status(paused=True)
            self._journal.append("pause")
            return True, "ok"
        if op == "resume":
            self._client.set_property("pause", False)
            self._set_status(paused=False)
            self._journal.append("resume")
            return True, "ok"
        if op == "stop_after":
            # toggle: pause playback when the CURRENT song ends (the next
            # song loads and holds at 0:00, ready for Go On Air)
            self._stop_after_current = not self._stop_after_current
            self._set_status(stop_after_current=self._stop_after_current)
            self._journal.append("stop_after_armed" if self._stop_after_current
                                 else "stop_after_cancelled")
            return True, "ok"
        if op == "emergency":
            # operator's big red button: hold on filler until resume_normal
            if not self._state.forced_emergency:
                self._state.forced_emergency = True
                self._store.save(self._state)
                self._set_status(forced_emergency=True)
                self._journal.append("emergency_forced")
                log.warning("operator FORCED emergency mode")
                self._enter_emergency("operator forced")
            return True, "ok"
        if op == "resume_normal":
            if self._state.forced_emergency:
                self._state.forced_emergency = False
                self._store.save(self._state)
                self._set_status(forced_emergency=False)
                self._journal.append("emergency_force_cleared")
                log.warning("operator cleared forced emergency")
                if self._state.emergency_mode:
                    # picks up the queue if playable; start-file exits
                    # emergency, otherwise filler keeps looping (correct)
                    self._play_next_emergency()
            return True, "ok"
        return False, f"unknown op {op!r}"

    # -------------------------------------------------------------- watchdog

    def _watchdog_loop(self) -> None:
        last_pos = None
        stall_ticks = 0
        while not self._stopping.wait(self._watchdog_interval):
            self._write_heartbeat()
            client = self._client
            if client is None:
                continue
            if not client.is_running() or not client.ping():
                self._set_status(mpv_alive=False)
                self._post({"kind": "mpv_stalled", "why": "process/IPC dead"})
                last_pos, stall_ticks = None, 0
                continue
            self._set_status(mpv_alive=True)
            try:
                paused = client.get_property("pause", timeout=1.0)
                idle = client.get_property("idle-active", timeout=1.0)
            except (MpvDead, MpvError):
                continue
            if paused or idle:
                last_pos, stall_ticks = None, 0
                continue
            pos = dur = None
            try:
                pos = client.get_property("time-pos", timeout=1.0)
                dur = client.get_property("duration", timeout=1.0)
            except MpvError:
                pass  # loading — track-change events cover this window
            except MpvDead:
                continue
            self._set_status(position=pos, duration=dur)
            if pos is not None and last_pos is not None and pos == last_pos:
                stall_ticks += 1
                if stall_ticks >= STALL_TICKS:
                    self._post({"kind": "mpv_stalled",
                                "why": f"position frozen at {pos}"})
                    stall_ticks = 0
            else:
                stall_ticks = 0
            last_pos = pos

    def _write_heartbeat(self) -> None:
        try:
            with open(self._heartbeat_path, "w") as f:
                f.write(str(time.time()))
        except OSError:
            log.warning("heartbeat write failed")

    # ------------------------------------------------------------------- mpv

    def _start_mpv(self) -> None:
        self._client = MpvClient(
            self._mpv_path, self._pipe_name,
            audio_device=self._audio_device,
            extra_args=self._extra_mpv_args,
            event_callback=self._on_mpv_event,
        )
        self._client.start()
        self._expected_next_path = None

    def _restart_mpv(self, why: str) -> None:
        now = time.monotonic()
        if now - self._last_restart < RESTART_BACKOFF:
            time.sleep(RESTART_BACKOFF - (now - self._last_restart))
        self._last_restart = time.monotonic()
        self._journal.append("mpv_restart", reason=why)
        log.error("restarting mpv: %s", why)
        # drop queued stale events/actions from the dead instance
        try:
            while True:
                self._actions.get_nowait()
        except queue_mod.Empty:
            pass
        old = self._client
        self._client = None
        if old is not None:
            try:
                old.stop(timeout=1.0)
            except Exception:
                log.exception("old mpv cleanup failed")
        self._start_mpv()
        self._kick(f"after mpv restart ({why})")

    # ----------------------------------------------------------------- misc

    def _pending_ids(self) -> list:
        """Ids of not-yet-played queue entries (P2 reconciles by identity)."""
        return [e.get("id")
                for e in self._state.entries[self._state.current_index + 1:]]

    def _try_get(self, prop: str):
        try:
            return self._client.get_property(prop)
        except (MpvError, MpvDead):
            return None

    def _set_status(self, **kv) -> None:
        with self._status_lock:
            self._status.update(kv)

    def _status_get(self, key: str):
        with self._status_lock:
            return self._status.get(key)
