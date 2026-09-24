"""P2 feeder concurrency regression (feeder hardening, TimeTrax #634).

Proves Feeder._lock closes the lost-update race: a slow NAS copy inside
tick()'s feed loop must never let a concurrent operator action (insert_spot /
insert_manual) get silently dropped from feeder_state, which would then let
_cache_lookahead/evict_except delete its just-cached file while it is still
queued in P1 (the original bug: P1 later skips it as "unplayable at
prefetch" — the DJ's cued track or a fired spot never airs, with no error
surfaced anywhere).

No real P1/mpv here on purpose — a stub engine (thread-safe, enforces the
queue_version protocol) isolates the thing under test: Feeder's own locking,
not P1's. See tests/test_engine_bridge.py for the real-mpv end-to-end.

Run: python tests/test_feeder_concurrency.py   (~5s)

Sanity check performed by hand while writing this test: with the
`with self._lock:` at the top of Feeder.tick() commented out, this test
FAILS (missing/evicted entries below). Restoring it makes it pass. See
REVIEW.md, feeder hardening entry, for the confirmation notes.
"""
import math
import os
import struct
import sys
import tempfile
import threading
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db, playlists as pl                 # noqa: E402
from services.core import engine_bridge                        # noqa: E402
from services.core.engine_bridge import Feeder, Precache       # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def make_wav(path, seconds=1.0, freq=440):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / 8000)))
            for i in range(int(8000 * seconds))))


class StubEngine:
    """A minimal P1 stand-in: accepts mutations, tracks pending ids, enforces
    the queue_version protocol. Thread-safe (its own lock, separate from
    Feeder's) so this test isolates Feeder's locking, not the stub's.

    Nothing ever "plays" here (current_index stays at -1) — every fed/
    inserted entry stays pending for the life of the test, which is exactly
    what lets the assertions below check "still present, never evicted"
    without a legitimate after-airplay eviction muddying the result."""

    def __init__(self):
        self._lock = threading.Lock()
        self.entries = []
        self.version = 0
        self.current_index = -1

    def status(self):
        with self._lock:
            return self._status_locked()

    def _status_locked(self):
        return {"now_playing": None, "now_title": None, "now_source": None,
                "now_id": None, "duration": None, "position": None,
                "paused": False, "stop_after_current": False,
                "emergency_mode": False, "forced_emergency": False,
                "mpv_alive": True, "queue_version": self.version,
                "current_index": self.current_index,
                "queue_len": len(self.entries),
                "pending_ids": [e["id"] for e in self.entries]}

    def queue(self, mutation):
        with self._lock:
            if mutation.get("queue_version") != self.version + 1:
                return 409, {"status": self._status_locked()}
            op = mutation.get("op")
            entries = mutation.get("entries", [])
            if op == "replace":
                self.entries = list(entries)
            elif op == "append":
                self.entries.extend(entries)
            elif op == "insert_next":
                at = self.current_index + 1
                self.entries[at:at] = entries
            else:
                return 400, {"error": f"unsupported op {op!r} in stub"}
            self.version += 1
            return 202, {}

    def op(self, op):
        return 200, {}


def main():
    td = tempfile.mkdtemp(prefix="sf-feeder-cc-")
    nas = os.path.join(td, "nas")
    os.makedirs(nas)
    base_tracks = []
    for i in range(6):
        p = os.path.join(nas, f"base{i}.wav")
        make_wav(p, 1.0, 300 + 40 * i)
        base_tracks.append(p)
    spot_tracks = [os.path.join(nas, f"spot{i}.wav") for i in range(20)]
    for i, p in enumerate(spot_tracks):
        make_wav(p, 0.3, 900 + 10 * i)

    db_path = os.path.join(td, "core.db")
    db.migrate(db_path)
    conn_setup = db.connect(db_path)
    pid = pl.create_playlist(conn_setup, "Base Rotation")
    for i, p in enumerate(base_tracks):
        pl.add_item(conn_setup, pid, "file", p, f"Base {i}")

    precache_dir = os.path.join(td, "precache")
    pc = Precache(precache_dir)

    # simulate a slow SMB copy: every ensure() call sleeps first, even a
    # cache hit — widens the race window tick() and the operator threads
    # must survive without losing bookkeeping
    real_ensure = Precache.ensure

    def slow_ensure(self, src):
        time.sleep(0.03)
        return real_ensure(self, src)

    Precache.ensure = slow_ensure

    # keep tick()'s feed loop doing real (slow) work on every call instead
    # of short-circuiting into the fast "topped up" path — feed_ahead huge
    # so it's never satisfied, batch size shrunk so each tick's window is a
    # bounded ~5*30ms instead of up to 50*30ms
    real_max_batch = engine_bridge.MAX_FEED_BATCH
    engine_bridge.MAX_FEED_BATCH = 5

    stub = StubEngine()
    cfg = {"precache_target_minutes": 0.05,  # ~3s — keeps the lookahead's
                                              # NAS-copy work small per tick
          "db_path": db_path, "feed_ahead_tracks": 100000}
    feeder = Feeder(cfg, stub, pc)

    try:
        feeder.activate(conn_setup, pid)  # single-threaded initial feed

        expected_paths = []
        errors = []

        def tick_worker():
            conn_a = db.connect(db_path)
            try:
                for _ in range(20):
                    ok, why = feeder.tick(conn_a)
                    if not ok:
                        errors.append(f"tick failed: {why}")
                    time.sleep(0.01)
            finally:
                conn_a.close()

        def insert_worker():
            conn_b = db.connect(db_path)
            try:
                for i, src in enumerate(spot_tracks):
                    if i % 2 == 0:
                        ok, why = feeder.insert_spot(conn_b, file_path=src,
                                                     label=f"Spot{i}")
                    else:
                        ok, why = feeder.insert_manual(conn_b, src,
                                                        title=f"Manual{i}")
                    if not ok:
                        errors.append(f"insert {i} failed: {why}")
                    else:
                        expected_paths.append(pc.cache_path_for(src))
                    time.sleep(0.01)
            finally:
                conn_b.close()

        ta = threading.Thread(target=tick_worker, name="tick-worker")
        tb = threading.Thread(target=insert_worker, name="insert-worker")
        ta.start()
        tb.start()
        ta.join(60)
        tb.join(60)

        check("tick thread finished", not ta.is_alive())
        check("insert thread finished", not tb.is_alive())
        check("no errors from either thread", not errors)
        check("all inserts reported ok",
              len(expected_paths) == len(spot_tracks))

        conn_check = db.connect(db_path)
        st = feeder._load_state(conn_check)
        fed_paths = {e["path"] for e in st["fed"]}

        missing = [p for p in expected_paths if p not in fed_paths]
        check("every inserted entry is still in feeder_state "
              "(no lost update)", not missing)

        evicted = [p for p in expected_paths if not os.path.exists(p)]
        check("no inserted file was evicted while still queued", not evicted)
        conn_check.close()

        # ---- eviction grace age (§2e): a fresh cache survives an immediate
        # eviction pass; min_age_sec=0 clears it as before
        probe_src = os.path.join(nas, "grace_probe.wav")
        make_wav(probe_src, 0.3, 500)
        cached = pc.ensure(probe_src)
        check("grace probe cached", cached and os.path.exists(cached))
        pc.evict_except(set())
        check("fresh cache survives immediate eviction (grace age)",
              os.path.exists(cached))
        pc.evict_except(set(), min_age_sec=0)
        check("min_age_sec=0 evicts immediately", not os.path.exists(cached))
    finally:
        Precache.ensure = real_ensure
        engine_bridge.MAX_FEED_BATCH = real_max_batch
        conn_setup.close()

    print(f"FEEDER CONCURRENCY OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
