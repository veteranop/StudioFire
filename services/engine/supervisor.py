"""P1 engine supervisor: two-deck playout, watchdog, failover chain.

Binding spec: PLAN.md §10.1/§10.2. P1 design laws: STDLIB ONLY.

Two decks (2026-09-25, true crossfade):
  Two mpv players, deck A and deck B, each with its own IPC pipe. Every item
  plays start to finish on ONE deck; the decks alternate. While one deck is
  on air, the next queue item is loaded PAUSED on the other deck (prefetch),
  so starting it is near-instant.
  - song -> anything: when the song reaches its last `crossfade_sec`, the
    next item starts on the other deck. A song rises from silence while the
    old song falls (a true overlapping crossfade); a spot/ID/PSA starts at
    FULL volume over the old song's fade-out — spots never fade.
  - spot -> anything: the next item starts the moment the spot ends, at full
    volume (no fade, no dip).
  - crossfade_sec = 0: no fades at all; items play back to back.
  Skip is a hard cut. Stop-after-current suppresses the crossfade so the
  song plays to its end and the next item waits, paused, at 0:00.

Threading model (keeps §10.2's single-writer guarantee):
- OWNER THREAD: the only thread that touches QueueState, decides what plays
  on which deck, and calls QueueStore.save(). It drains an action queue.
- WATCHDOG THREAD: every ~1s checks both players' liveness + the on-air
  deck's position advance; posts actions; writes the heartbeat file.
- MIXER THREAD: every 0.1s ramps the volume of decks that are fading and
  tells the owner when a song reaches its crossfade point. It never changes
  what is playing — only volume.
- mpv I/O threads (inside MpvClient) post mpv events as actions.

Failover chain (single rule, §10.1): any condition where the next track
cannot start -> emergency folder loop -> cached music (precache dir,
scanned fresh — its contents churn) -> baked-in last-resort source.
Exit emergency as soon as a playable queue item exists again. Filler never
crossfades (it plays on the on-air deck, back to back).

The baked-in tier defaults to an ffmpeg-generated tone (av://lavfi) so it
exists even if every file on disk is gone; a real station-ID file can be
configured instead (config key engine.baked_in_asset).
"""

from __future__ import annotations

import logging
import math
import os
import queue as queue_mod
import subprocess
import threading
import time

from .journal import Journal
from .mpv_ipc import MpvClient, MpvDead, MpvError, kill_stale_mpv
from .queue_store import QueueStore, QueueState, apply_mutation

log = logging.getLogger("engine.supervisor")

BAKED_IN_DEFAULT = "av://lavfi:sine=frequency=600:sample_rate=48000"
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
              ".wma", ".aiff", ".aif"}
STALL_TICKS = 2          # position frozen for N watchdog ticks -> restart mpv
RESTART_BACKOFF = 1.0    # seconds between consecutive mpv restarts
MAX_QUEUE_HISTORY = 20   # played entries kept in the runtime queue (journal is
                         # the permanent record); older ones are trimmed
FULL_VOLUME = 100
MIX_TICK = 0.1           # mixer resolution (seconds)
IDLE_TICKS = 2           # on-air deck idle this many watchdog ticks -> kick
# queue sources that crossfade. Spots (IDs, ads, PSAs) are deliberately NOT
# here — they play at full volume start to finish. Emergency/baked-in filler
# isn't a queue entry, so it never fades either.
FADE_SOURCES = {"playlist", "show", "manual"}
# a file shorter than this many crossfades doesn't crossfade (it would spend
# its whole life fading)
MIN_XFADE_MULT = 2.0
# On Air level meter: a measure-only ffmpeg filter on each deck (per-frame
# peak + RMS per channel, read via mpv's af-metadata/meter property)
METER_FILTER = ("@meter:lavfi=[astats=metadata=1:reset=1:"
                "measure_perchannel=Peak_level+RMS_level:measure_overall=none]")
METER_FLOOR_DB = -90.0
# an outgoing song's fade-out reaches silence this long before its end-of-file
# (mpv's buffered tail can't be ramped: time-pos isn't readable while it drains)
FADE_END_EARLY = 0.5


def _equal_power(x: float, full: float = FULL_VOLUME) -> float:
    """mpv volume for fade progress x (0 = silent .. 1 = full) on an
    EQUAL-POWER curve: gain = sin(x·π/2), so an outgoing cos and incoming
    sin sum to constant loudness — no dip mid-crossfade. mpv's volume is
    cubic (gain = (v/100)³), so volume = full · gain^(1/3). A straight line
    in volume would leave both songs at 50 (≈ -18 dB each, ~-15 dB
    combined) at the midpoint: an audible dip."""
    x = min(1.0, max(0.0, x))
    return full * math.sin(x * math.pi / 2) ** (1.0 / 3.0)


def fade_volume(pos, dur, fade_in: float, fade_out: float,
                full: float = FULL_VOLUME) -> float:
    """Volume at position `pos` of a `dur`-second file: rises over the first
    `fade_in` seconds, falls over the last `fade_out`, equal-power. Unknown
    pos/dur -> full volume (never guess toward silence)."""
    if pos is None:
        return full
    vol = full
    if fade_in > 0 and pos < fade_in:
        vol = min(vol, _equal_power(pos / fade_in, full))
    if fade_out > 0 and dur:
        left = dur - pos
        if left < fade_out:
            vol = min(vol, _equal_power(left / fade_out, full))
    return vol


def _db(v) -> float | None:
    """astats metadata value ('-12.3', '-inf') -> float dB, or None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isinf(f) or math.isnan(f) else f


def _add_db(level: float | None, gain_db: float) -> float | None:
    if level is None or math.isinf(gain_db):
        return None
    return level + gain_db


def _floor(db: float | None) -> float | None:
    """Below METER_FLOOR_DB counts as silence; round for the wire."""
    return None if db is None or db < METER_FLOOR_DB else round(db, 1)


def _same_path(a, b) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.normpath(a)) == \
        os.path.normcase(os.path.normpath(b))


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


class Deck:
    """One mpv player. Plays exactly one file at a time.

    role: 'idle'      nothing loaded
          'preloaded' next queue item loaded PAUSED, waiting its turn
          'onair'     the item listeners are hearing as "now playing"
          'fading'    the previous song, fading out under the new one
    Only the owner thread changes these fields (the mixer reads `fade`
    under the supervisor's mix lock)."""

    def __init__(self, name: str, pipe_name: str):
        self.name = name
        self.pipe_name = pipe_name
        self.client: MpvClient | None = None
        self.role = "idle"
        self.path: str | None = None
        self.entry: dict | None = None     # queue entry, None = filler
        self.fade: dict | None = None      # {"in": sec} / {"out": sec}
        self.xfade_asked = False           # mixer already reported the point
        # mpv's playlist_entry_id of the file now loaded: end-file events for
        # any OTHER (earlier) file on this deck are stale and ignored
        self.mpv_entry_id = None
        # the volume we last set on this player (the level meter reads the
        # signal BEFORE mpv's volume, so it scales by this)
        self.volume = FULL_VOLUME
        self.meter = False                 # level-meter filter attached

    def reset(self) -> None:
        self.role, self.path, self.entry = "idle", None, None
        self.fade, self.xfade_asked = None, False
        self.mpv_entry_id = None

    def __repr__(self):
        return f"<deck {self.name} {self.role} {os.path.basename(self.path or '')}>"


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
        # crossfade length in seconds; 0 = back to back, no fades
        self._xfade = max(0.0, float(c.get("crossfade_sec", 0) or 0))
        # On Air level meter (measure-only filter on each deck)
        self._meter_on = bool(c.get("level_meter", True))
        self._levels_lock = threading.Lock()
        self._levels: dict = {"channels": [{"peak": None, "rms": None}] * 2,
                              "decks": 0, "ts": 0.0}

        self._store = QueueStore(c["state_path"])
        self._journal = Journal(c["journal_path"])
        self._state: QueueState = QueueState()

        self._actions: queue_mod.Queue = queue_mod.Queue()
        self._decks = [Deck("A", self._pipe_name + "-a"),
                       Deck("B", self._pipe_name + "-b")]
        self._onair: Deck | None = None
        self._mix_lock = threading.Lock()
        self._owner: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._mixer: threading.Thread | None = None
        self._stopping = threading.Event()

        self._emergency_files: list[str] = []
        self._emergency_idx = 0
        self._stop_after_current = False  # pause when the current song ends
        self._paused = False
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

    # the on-air player (tests reach in here to kill mpv mid-track)
    @property
    def _client(self) -> MpvClient | None:
        d = self._onair or self._decks[0]
        return d.client

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
        # a previous engine that died without its players leaves them running
        # on our pipe names; clear them BEFORE starting ours, or we may end up
        # driving an orphan (see mpv_ipc.kill_stale_mpv). The bare pipe name
        # is the pre-two-deck engine's.
        for pipe in (self._pipe_name, *(d.pipe_name for d in self._decks)):
            stale = kill_stale_mpv(pipe)
            if stale:
                self._journal.append("stale_mpv_killed", pids=stale)
        for d in self._decks:
            self._start_deck(d)
        # both players answered their IPC pipes — they're alive now, not
        # just after the watchdog's first tick (status must be truthful from
        # the first moment P2 or anyone can see playback)
        self._set_status(mpv_alive=True)
        self._owner = threading.Thread(target=self._owner_loop,
                                       name="engine-owner", daemon=True)
        self._owner.start()
        self._watchdog = threading.Thread(target=self._watchdog_loop,
                                          name="engine-watchdog", daemon=True)
        self._watchdog.start()
        self._mixer = threading.Thread(target=self._mixer_loop,
                                       name="engine-mixer", daemon=True)
        self._mixer.start()
        # resume where we left off (or re-enter emergency per persisted flag)
        self._post({"kind": "kick", "why": "startup"})

    def stop(self) -> None:
        self._stopping.set()
        self._post({"kind": "shutdown"})
        for t, wait in ((self._owner, 5), (self._watchdog, 2),
                        (self._mixer, 2)):
            if t:
                t.join(wait)
        for d in self._decks:
            if d.client:
                d.client.stop()
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
        """skip / pause / resume / stop_after / emergency / resume_normal."""
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
                    self._handle_mpv_event(action)
                elif kind == "mutation":
                    ok, why = self._handle_mutation(action["mutation"])
                    action["result"][:] = [ok, why]
                    action["done"].set()
                elif kind == "op":
                    ok, why = self._handle_op(action["op"])
                    action["result"][:] = [ok, why]
                    action["done"].set()
                elif kind == "xfade_due":
                    self._on_xfade_due(action["deck"], action["client"])
                elif kind == "deck_dead":
                    self._restart_deck(action["deck"], action.get("why", ""),
                                       action.get("client"))
                elif kind == "kick":
                    self._kick(action.get("why", ""))
            except MpvDead as exc:
                log.error("mpv died during '%s': %s", kind, exc)
                # restart the deck(s) whose player is actually gone — not
                # blindly the on-air one
                dead = [d for d in self._decks
                        if d.client is None or not d.client.is_running()]
                for d in dead or [self._onair or self._decks[0]]:
                    self._restart_deck(d, f"MpvDead during {kind}")
                if not dead and self._onair is None:
                    self._kick(f"after MpvDead during {kind}")
            except Exception:
                log.exception("owner loop error handling %s", kind)
                # Never let a logic bug kill the loop; failover keeps air alive.
                self._safe_enter_emergency("owner loop exception")
            finally:
                # a mutation/op must never leave its caller waiting on a
                # failed handler
                if kind in ("mutation", "op") and "done" in action:
                    action["done"].set()

    # ----------------------------------------------------------- mpv events

    def _event_cb(self, deck: Deck, client_ref: list):
        def cb(msg: dict) -> None:  # runs on that deck's mpv I/O thread
            if msg.get("event") == "end-file":
                self._post({"kind": "mpv_event", "deck": deck,
                            "client": client_ref[0], "msg": msg})
        return cb

    def _handle_mpv_event(self, action: dict) -> None:
        deck, msg = action["deck"], action["msg"]
        if action["client"] is not deck.client:
            return                       # from a player we've since replaced
        eid = msg.get("playlist_entry_id")
        if deck.mpv_entry_id is not None and eid is not None \
                and eid != deck.mpv_entry_id:
            return                       # an earlier file on this deck
        reason = msg.get("reason", "unknown")
        if reason not in ("eof", "error"):
            return                       # 'stop'/'quit'/'redirect': we did it
        path = deck.path
        self._journal.append("track_end", path=path, reason=reason,
                             error=msg.get("file_error"))
        if reason == "error":
            log.error("decode/play error on deck %s %s: %s", deck.name, path,
                      msg.get("file_error"))
        role = deck.role
        if role == "fading":             # the old song's tail finished
            deck.reset()
            self._ensure_preloaded()
            return
        if role == "preloaded":          # the NEXT item failed to even load
            e = deck.entry
            deck.reset()
            if e is not None:
                self._drop_pending(e, "unplayable at prefetch")
            self._ensure_preloaded()
            return
        if role == "onair":
            self._on_onair_ended(deck)

    def _on_onair_ended(self, deck: Deck) -> None:
        """The item on air finished (or failed) with no crossfade — start
        the next thing right now (§10.1: never silent)."""
        deck.reset()
        if self._onair is deck:
            self._onair = None
        if self._state.emergency_mode:
            self._play_next_emergency()
            return
        self._advance_or_fail("track ended", hold=self._stop_after_current)

    # ----------------------------------------------------------------- decks

    def _start_deck(self, deck: Deck) -> None:
        ref: list = [None]
        client = MpvClient(
            self._mpv_path, deck.pipe_name,
            audio_device=self._audio_device,
            extra_args=self._extra_mpv_args,
            event_callback=self._event_cb(deck, ref),
        )
        ref[0] = client
        client.start()
        client.set_property("pause", True)
        deck.meter = False
        if self._meter_on:
            # measure-only filter for the On Air level meter, attached at
            # runtime so a player that can't do it just has no meter —
            # playback is never affected
            try:
                client.command("af", "set", METER_FILTER)
                deck.meter = True
            except MpvError as exc:
                log.warning("deck %s: level meter unavailable (%s)",
                            deck.name, exc)
        deck.client = client
        deck.volume = FULL_VOLUME
        deck.reset()

    def _other(self, deck: Deck | None) -> Deck:
        return self._decks[1] if deck is self._decks[0] else self._decks[0]

    def _free_deck(self) -> Deck | None:
        """A deck that is not on air and not fading — the one holding the
        preloaded next item first, else an idle one."""
        for want in ("preloaded", "idle"):
            for d in self._decks:
                if d is not self._onair and d.role == want:
                    return d
        return None

    def _load(self, deck: Deck, path: str, entry: dict | None,
              role: str) -> None:
        """Load a file PAUSED on a deck (volume is set when it goes on air)."""
        deck.client.set_property("pause", True)
        res = deck.client.command("loadfile", path, "replace")
        deck.client.set_property("loop-file", "no")
        deck.path, deck.entry, deck.role = path, entry, role
        deck.fade, deck.xfade_asked = None, False
        deck.mpv_entry_id = res.get("playlist_entry_id") \
            if isinstance(res, dict) else None

    def _go_on_air(self, deck: Deck, path: str, entry: dict | None,
                   fade_in: float = 0.0, hold: bool = False) -> None:
        """Make `deck` the on-air deck playing `path` (loading it unless it's
        already preloaded there), starting at silence for a fade-in or at
        full volume, and publish it as now playing."""
        if not (deck.role == "preloaded" and _same_path(deck.path, path)):
            self._load(deck, path, entry, "onair")
        start_vol = 0 if fade_in > 0 else FULL_VOLUME
        with self._mix_lock:
            deck.role, deck.entry, deck.path = "onair", entry, path
            deck.fade = {"in": fade_in} if fade_in > 0 else None
            deck.xfade_asked = False
            deck.client.set_property("volume", start_vol)
            deck.volume = start_vol
        if not hold and not self._paused:
            deck.client.set_property("pause", False)
        self._onair = deck
        self._announce(deck, path, entry)
        if hold:
            self._stop_after_current = False
            self._paused = True
            self._journal.append("stopped_after_song", path=path)
            log.warning("stopped on air after operator's stop-after request")
            self._set_status(stop_after_current=False, paused=True)
        self._ensure_preloaded()

    def _announce(self, deck: Deck, path: str, entry: dict | None) -> None:
        """Bookkeeping for 'this is now on air' (was start-file handling)."""
        if entry is not None:
            idx = self._index_of(entry)
            if idx is not None and idx != self._state.current_index:
                self._state.current_index = idx
            self._state.trim_history(MAX_QUEUE_HISTORY)
            self._store.save(self._state)
            if self._state.emergency_mode:
                self._exit_emergency(reason="queue track started")
            self._journal.append("track_start", path=path,
                                 title=entry.get("title"),
                                 source=entry.get("source"))
            self._set_status(current_index=self._state.current_index,
                             queue_len=len(self._state.entries),
                             pending_ids=self._pending_ids(),
                             now_title=entry.get("title"),
                             now_source=entry.get("source"),
                             now_id=entry.get("id"))
        else:
            source = "emergency" if path != self._baked_in else "baked_in"
            self._journal.append("track_start", path=path, source=source)
            self._set_status(now_title=None, now_source=source, now_id=None)
        self._set_status(now_playing=path, duration=None, position=None)

    def _stop_deck(self, deck: Deck) -> None:
        try:
            deck.client.set_property("pause", True)
            deck.client.command("stop")
        except MpvError:
            pass
        with self._mix_lock:
            deck.reset()
        if self._onair is deck:
            self._onair = None

    def _ensure_preloaded(self) -> None:
        """Keep the next playable queue item loaded, paused, on the free deck
        (the two-deck equivalent of mpv's playlist prefetch)."""
        if self._state.emergency_mode or self._state.forced_emergency:
            return
        nxt = self._state.next_entry()
        while nxt is not None and not playable(nxt["path"]):
            self._drop_pending(nxt, "unplayable at prefetch")
            nxt = self._state.next_entry()
        deck = self._free_deck()
        if deck is None:
            return          # other deck still fading; retried when it ends
        if nxt is None:
            if deck.role == "preloaded":
                self._stop_deck(deck)       # what it held is gone
            return
        if deck.role == "preloaded" and deck.entry is not None and \
                deck.entry.get("id") == nxt.get("id") and \
                _same_path(deck.path, nxt["path"]):
            return          # already primed
        try:
            self._load(deck, nxt["path"], nxt, "preloaded")
        except MpvError as exc:
            log.warning("preload on deck %s failed (%s); will load at switch",
                        deck.name, exc)
            deck.reset()

    def _drop_pending(self, entry: dict, reason: str) -> None:
        self._journal.append("track_skip", path=entry.get("path"),
                             reason=reason)
        cut = self._state.current_index + 1
        self._state.entries[cut:] = [e for e in self._state.entries[cut:]
                                     if e is not entry]
        self._store.save(self._state)
        self._set_status(queue_len=len(self._state.entries),
                         pending_ids=self._pending_ids())

    def _index_of(self, entry: dict) -> int | None:
        for i, e in enumerate(self._state.entries):
            if e is entry or (entry.get("id") is not None
                              and e.get("id") == entry.get("id")):
                return i
        return None

    # ------------------------------------------------------------ crossfade

    def _crossfades(self, deck: Deck) -> bool:
        return (self._xfade > 0 and deck.entry is not None
                and deck.entry.get("source") in FADE_SOURCES)

    def _on_xfade_due(self, deck: Deck, client) -> None:
        """The on-air song reached its crossfade point: start the next item
        on the other deck now, and fade this song out underneath it."""
        if deck is not self._onair or client is not deck.client \
                or deck.role != "onair" or not self._crossfades(deck):
            return
        if self._stop_after_current or self._paused or \
                self._state.emergency_mode or self._state.forced_emergency:
            return          # plays to its end; eof handles what comes next
        nxt = self._state.next_entry()
        other = self._other(deck)
        if nxt is None or other.role not in ("idle", "preloaded") \
                or not playable(nxt["path"]):
            return          # nothing ready: normal end-of-song advance
        # old song: fade out over whatever is left of it
        try:
            pos = deck.client.get_property("time-pos", timeout=0.5)
            dur = deck.client.get_property("duration", timeout=0.5)
        except MpvError:
            return
        left = max(0.1, (dur or 0) - (pos or 0))
        # finish the fade-out FADE_END_EARLY before end-of-file: mpv's last
        # few hundred ms drain from its audio buffer where time-pos can't be
        # read, so a ramp aimed at the very end would stall around 75%
        out_len = max(0.1, min(self._xfade, left - FADE_END_EARLY))
        with self._mix_lock:
            deck.role = "fading"
            deck.fade = {"out": out_len, "end_early": FADE_END_EARLY}
        self._onair = None
        # new item: a song rises from silence, a spot starts at full
        fade_in = self._xfade if nxt.get("source") in FADE_SOURCES else 0.0
        self._journal.append("crossfade", from_path=deck.path,
                             to_path=nxt["path"], seconds=round(left, 1))
        self._go_on_air(other, nxt["path"], nxt, fade_in=fade_in)

    def _mixer_loop(self) -> None:
        """Volume ramps for fading decks + crossfade-point detection. Only
        ever touches volume; every error path falls back to full volume for
        the on-air deck."""
        while not self._stopping.wait(MIX_TICK):
            levels: list[list[tuple]] = []      # per audible deck
            for deck in list(self._decks):
                client = deck.client
                if client is None:
                    continue
                with self._mix_lock:
                    role, fade = deck.role, dict(deck.fade or {})
                    asked = deck.xfade_asked
                try:
                    if role == "onair" and not asked and \
                            self._crossfades(deck):
                        self._check_xfade_point(deck, client)
                    if role not in ("onair", "fading"):
                        continue
                    if client.get_property("pause", timeout=0.5):
                        continue
                    if fade and not self._ramp(deck, client, role, fade):
                        continue                # changed under us
                    if deck.meter:
                        lv = self._read_meter(deck, client)
                        if lv:
                            levels.append(lv)
                except MpvError:
                    continue    # e.g. time-pos unavailable while loading
                except MpvDead:
                    continue    # watchdog restarts that deck
                except Exception:
                    log.exception("mixer error on deck %s", deck.name)
                    if deck is self._onair:
                        try:
                            client.set_property("volume", FULL_VOLUME)
                            deck.volume = FULL_VOLUME
                        except (MpvDead, MpvError):
                            pass
            if self._meter_on:
                self._publish_levels(levels)

    def _ramp(self, deck: Deck, client, role: str, fade: dict) -> bool:
        """One step of a deck's fade. False = the deck changed under us."""
        pos = client.get_property("time-pos", timeout=0.5)
        dur = client.get_property("duration", timeout=0.5)
        if dur and fade.get("end_early"):
            dur = max(0.0, dur - fade["end_early"])   # reach 0 a bit early
        vol = fade_volume(pos, dur, fade.get("in", 0.0), fade.get("out", 0.0))
        with self._mix_lock:
            if deck.client is not client or deck.fade is None \
                    or deck.role != role:
                return False
            if "in" in fade and pos is not None and pos >= fade["in"]:
                deck.fade = None            # fade-in complete
                vol = FULL_VOLUME
            client.set_property("volume", round(vol), timeout=0.5)
            deck.volume = round(vol)
        return True

    # ---------------------------------------------------------- level meter

    def _read_meter(self, deck: Deck, client) -> list[tuple] | None:
        """[(peak_db, rms_db), ...] per channel for one audible deck, as heard:
        the filter measures before mpv's volume, so scale by the deck's
        volume (mpv's volume curve is cubic: gain = (v/100)^3)."""
        md = client.get_property("af-metadata/meter", timeout=0.3)
        if not isinstance(md, dict):
            return None
        vol = max(0.0, float(deck.volume)) / FULL_VOLUME
        gain_db = 60.0 * math.log10(vol) if vol > 0 else -math.inf
        chans = []
        for ch in range(1, 9):
            peak = _db(md.get(f"lavfi.astats.{ch}.Peak_level"))
            rms = _db(md.get(f"lavfi.astats.{ch}.RMS_level"))
            if peak is None and rms is None:
                break
            chans.append((_add_db(peak, gain_db), _add_db(rms, gain_db)))
        return chans or None

    def _publish_levels(self, decks: list[list[tuple]]) -> None:
        """Combine every audible deck into one program level per channel:
        peaks take the max, RMS adds as power (two decks mid-crossfade)."""
        out = []
        for ch in range(2):
            peaks, powers = [], []
            for chans in decks:
                p, r = chans[min(ch, len(chans) - 1)]   # mono feeds both
                if p is not None:
                    peaks.append(p)
                if r is not None:
                    powers.append(10 ** (r / 10.0))
            peak = max(peaks) if peaks else None
            rms = 10 * math.log10(sum(powers)) if powers and sum(powers) > 0 \
                else None
            out.append({"peak": _floor(peak), "rms": _floor(rms)})
        with self._levels_lock:
            self._levels = {"channels": out, "decks": len(decks),
                            "ts": time.time()}

    def levels(self) -> dict:
        """Latest program level for the On Air meter (dBFS; None = silence).
        `enabled` False = this engine/mpv can't meter."""
        with self._levels_lock:
            lv = dict(self._levels)
        lv["enabled"] = self._meter_on and any(d.meter for d in self._decks)
        lv["paused"] = self._paused
        return lv

    def _check_xfade_point(self, deck: Deck, client) -> None:
        pos = client.get_property("time-pos", timeout=0.5)
        dur = client.get_property("duration", timeout=0.5)
        if pos is None or not dur or dur < self._xfade * MIN_XFADE_MULT:
            return
        if dur - pos <= self._xfade:
            with self._mix_lock:
                if deck.client is not client or deck.role != "onair" \
                        or deck.xfade_asked:
                    return
                deck.xfade_asked = True
            self._post({"kind": "xfade_due", "deck": deck, "client": client})

    # ------------------------------------------------------- queue mechanics

    def _advance_or_fail(self, why: str, hold: bool = False) -> None:
        """Start the next playable queue item NOW on a free deck at full
        volume, cutting whatever is on air (§10.1). `hold` = load it but stay
        paused (stop-after-current)."""
        if self._state.emergency_mode:
            self._play_next_emergency()
            return
        if self._onair is not None:
            self._stop_deck(self._onair)
        idx = self._state.current_index + 1
        while idx < len(self._state.entries):
            e = self._state.entries[idx]
            if playable(e["path"]):
                deck = self._free_deck() or self._decks[0]
                self._go_on_air(deck, e["path"], e, hold=hold)
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
        for d in self._decks:                 # filler owns the air now
            if d.role == "preloaded":
                self._stop_deck(d)
        self._play_next_emergency()

    def _play_next_emergency(self) -> None:
        # first, has P2 given us something playable in the meantime?
        # (unless the operator FORCED emergency — then stay on filler
        # until an explicit resume_normal)
        if not self._state.forced_emergency:
            nxt = self._state.next_entry()
            if nxt is not None and playable(nxt["path"]):
                if self._onair is not None:
                    self._stop_deck(self._onair)
                deck = self._free_deck() or self._decks[0]
                self._go_on_air(deck, nxt["path"], nxt)   # exits emergency
                return
        if self._onair is not None:
            self._stop_deck(self._onair)
        deck = self._free_deck() or self._decks[0]
        candidates = self._emergency_candidates()
        if candidates:
            p = candidates[self._emergency_idx % len(candidates)]
            self._emergency_idx += 1
            self._go_on_air(deck, p, None)
        else:
            # tier 3: baked-in source, looped — the last line of defense
            self._go_on_air(deck, self._baked_in, None)
            deck.client.set_property("loop-file", "inf")

    def _exit_emergency(self, reason: str) -> None:
        self._state.emergency_mode = False
        self._state.forced_emergency = False
        self._store.save(self._state)
        self._set_status(emergency_mode=False, forced_emergency=False)
        self._journal.append("emergency_exit", reason=reason)
        log.warning("exited emergency mode: %s", reason)

    def _safe_enter_emergency(self, why: str) -> None:
        try:
            self._enter_emergency(why)
        except Exception:
            log.exception("failover itself failed; restarting on-air deck")
            try:
                self._restart_deck(self._onair or self._decks[0],
                                   "failover failure")
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
            if self._state.forced_emergency:
                pass  # queued for later; operator holds us on filler
            elif mutation.get("op") == "replace":
                for d in self._decks:
                    if d.role == "fading":
                        self._stop_deck(d)
                self._advance_or_fail("queue replaced")
            elif self._state.emergency_mode:
                self._play_next_emergency()  # new material may end emergency
            elif self._onair is None:
                self._advance_or_fail("mutation while idle")
            else:
                self._ensure_preloaded()     # the next item may have changed
        return ok, why

    def _handle_op(self, op: str) -> tuple[bool, str]:
        if op == "skip":
            # a hard cut: the fading tail goes too
            for d in self._decks:
                if d.role == "fading":
                    self._stop_deck(d)
            self._advance_or_fail("operator skip")
            return True, "ok"
        if op == "pause":
            self._paused = True
            for d in self._decks:
                if d.role in ("onair", "fading"):
                    d.client.set_property("pause", True)
            self._set_status(paused=True)
            self._journal.append("pause")
            return True, "ok"
        if op == "resume":
            self._paused = False
            for d in self._decks:
                if d.role in ("onair", "fading"):
                    d.client.set_property("pause", False)
            self._set_status(paused=False)
            self._journal.append("resume")
            if self._onair is None:
                self._advance_or_fail("resume with nothing on air")
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
                for d in self._decks:
                    if d.role == "fading":
                        self._stop_deck(d)
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
                    # picks up the queue if playable, which exits emergency;
                    # otherwise filler keeps looping (correct)
                    self._play_next_emergency()
            return True, "ok"
        return False, f"unknown op {op!r}"

    # -------------------------------------------------------------- watchdog

    def _watchdog_loop(self) -> None:
        last_pos = None
        stall_ticks = 0
        idle_ticks = 0
        while not self._stopping.wait(self._watchdog_interval):
            self._write_heartbeat()
            alive = True
            for d in list(self._decks):
                c = d.client
                if c is None:
                    continue
                if not c.is_running() or not c.ping():
                    alive = False
                    self._post({"kind": "deck_dead", "deck": d, "client": c,
                                "why": "process/IPC dead"})
            self._set_status(mpv_alive=alive)
            deck = self._onair
            if not alive or deck is None or deck.client is None:
                last_pos, stall_ticks = None, 0
                # nothing on air while not paused -> make sure we recover
                if deck is None and alive and not self._paused:
                    idle_ticks += 1
                    if idle_ticks >= IDLE_TICKS:
                        self._post({"kind": "kick",
                                    "why": "nothing on air (watchdog)"})
                        idle_ticks = 0
                continue
            idle_ticks = 0
            client = deck.client
            try:
                paused = client.get_property("pause", timeout=1.0)
            except (MpvDead, MpvError):
                continue
            if paused:
                last_pos, stall_ticks = None, 0
                continue
            pos = dur = None
            try:
                pos = client.get_property("time-pos", timeout=1.0)
                dur = client.get_property("duration", timeout=1.0)
            except MpvError:
                pass  # loading — end-file events cover this window
            except MpvDead:
                continue
            self._set_status(position=pos, duration=dur)
            if pos is not None and last_pos is not None and pos == last_pos:
                stall_ticks += 1
                if stall_ticks >= STALL_TICKS:
                    self._post({"kind": "deck_dead", "deck": deck,
                                "client": client,
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

    def _restart_deck(self, deck: Deck, why: str, client=None) -> None:
        """Replace a dead/stalled player. If it was on air, carry on with the
        next item (same as the single-player engine did)."""
        if client is not None and client is not deck.client:
            return                          # already replaced
        now = time.monotonic()
        if now - self._last_restart < RESTART_BACKOFF:
            time.sleep(RESTART_BACKOFF - (now - self._last_restart))
        self._last_restart = time.monotonic()
        self._journal.append("mpv_restart", reason=why, deck=deck.name)
        log.error("restarting deck %s mpv: %s", deck.name, why)
        was_onair = deck is self._onair or deck.role == "onair"
        old = deck.client
        with self._mix_lock:
            deck.client = None
            deck.reset()
        if self._onair is deck:
            self._onair = None
        if old is not None:
            try:
                old.stop(timeout=1.0)
            except Exception:
                log.exception("old mpv cleanup failed")
        self._start_deck(deck)
        if was_onair:
            self._kick(f"after mpv restart ({why})")
        else:
            self._ensure_preloaded()

    # ----------------------------------------------------------------- misc

    def _pending_ids(self) -> list:
        """Ids of not-yet-played queue entries (P2 reconciles by identity)."""
        return [e.get("id")
                for e in self._state.entries[self._state.current_index + 1:]]

    def _set_status(self, **kv) -> None:
        with self._status_lock:
            self._status.update(kv)

    def _status_get(self, key: str):
        with self._status_lock:
            return self._status.get(key)
