"""Regression: a fired spot (202-accepted by P1) must never vanish from the
feeder's model — and so must never have its cached file evicted — before P1
has actually observed it as pending / on air.

Live incident (KDPI, 2026-09-29, TimeTrax H2627206): after the v1.2.0 Z:-drop
fix, spot rules fired again and P1 returned 202, but the spots never aired.
`play_history` showed zero source='spot' rows; the boundary after each fire
played the next SHOW/rotation item instead. Root cause: tick()'s reconcile
rebuilt feeder_state["fed"] purely from the engine's latest `pending_ids`
snapshot; any snapshot that did not yet list a just-inserted spot dropped that
spot's `fed` entry, and the eviction pass at the end of the same tick — keyed
off `fed` — then deleted the spot's precached file. An hourly ID/PSA is cached
with an OLD cached_at (ensure() doesn't re-stamp a still-valid cache), so the
600 s eviction grace never protected it. P1 then hit the just-deleted file at
prefetch, logged "unplayable at prefetch", and skipped straight to the next
item — the spot silently never aired.

Invariant under test: an item the engine has accepted (202) may not be removed
from the feeder model before it has been observed as started or ended.

No real P1/mpv: a stub engine lets us drive the exact race — a status snapshot
whose pending_ids do NOT yet include the freshly-inserted spot — deterministically.

Run: python tests/test_spot_airing.py   (~1s)

Sanity check performed while writing this test: against the pre-fix reconcile
(rebuild `fed` from pending_ids alone, no pin), the two "survives the stale
snapshot" checks below FAIL (spot missing from fed, cached file evicted).
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
from services.core.engine_bridge import Feeder, Precache       # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def make_wav(path, seconds=0.3, freq=440):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / 8000)))
            for i in range(int(8000 * seconds))))


class StubEngine:
    """P1 stand-in that models the pieces tick()/insert_spot() reconcile
    against: an ordered queue, a play-head (current_index), the queue_version
    protocol, and insert_next placing an entry right after the play-head.

    `hidden` is the test's lever: ids in it are withheld from status()'s
    pending_ids even though they are still in the queue and will still play —
    i.e. the exact window where P1 has accepted a spot (202) but a status
    snapshot doesn't list it yet."""

    def __init__(self):
        self._lock = threading.Lock()
        self.entries = []
        self.version = 0
        self.current_index = -1
        self.hidden = set()

    def _status_locked(self):
        ci = self.current_index
        now = self.entries[ci] if 0 <= ci < len(self.entries) else None
        pending = [e["id"] for e in self.entries[ci + 1:]
                   if e["id"] not in self.hidden]
        return {"now_playing": now["path"] if now else None,
                "now_title": now.get("title") if now else None,
                "now_source": now.get("source") if now else None,
                "now_id": now["id"] if now else None,
                "duration": None, "position": None, "paused": False,
                "stop_after_current": False, "emergency_mode": False,
                "forced_emergency": False, "mpv_alive": True,
                "queue_version": self.version, "current_index": ci,
                "queue_len": len(self.entries), "pending_ids": pending}

    def status(self):
        with self._lock:
            return self._status_locked()

    def queue(self, mutation):
        with self._lock:
            if mutation.get("queue_version") != self.version + 1:
                return 409, {"status": self._status_locked()}
            op = mutation.get("op")
            entries = mutation.get("entries", [])
            if op == "replace":
                self.entries = list(entries)
                self.current_index = -1
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

    # ---- test helpers (would be P1's playout in real life) ----
    def spot_id(self):
        for e in self.entries:
            if e.get("source") == "spot":
                return e["id"]
        return None


def find_fed(feeder, conn, entry_id):
    st = feeder._load_state(conn)
    return next((e for e in st["fed"] if e["id"] == entry_id), None)


def age_cache(pc, path, seconds_old=10000.0):
    """Backdate a cached file's manifest cached_at so the eviction grace
    (min_age_sec) no longer protects it — mimics an ID/PSA cached hours ago."""
    with pc._lock:
        pc._manifest["files"][path]["cached_at"] = time.time() - seconds_old
        pc._write_manifest()


def main():
    td = tempfile.mkdtemp(prefix="sf-spot-air-")
    nas = os.path.join(td, "nas")
    os.makedirs(nas)
    base_tracks = []
    for i in range(5):
        p = os.path.join(nas, f"base{i}.wav")
        make_wav(p, 0.3, 300 + 40 * i)
        base_tracks.append(p)
    spot_src = os.path.join(nas, "legal_id.wav")
    make_wav(spot_src, 0.3, 990)

    db_path = os.path.join(td, "core.db")
    db.migrate(db_path)
    conn = db.connect(db_path)
    pid = pl.create_playlist(conn, "Base Rotation")
    for i, p in enumerate(base_tracks):
        pl.add_item(conn, pid, "file", p, f"Base {i}")

    pc = Precache(os.path.join(td, "precache"))
    cfg = {"precache_target_minutes": 0.02, "db_path": db_path,
           "feed_ahead_tracks": 3}
    stub = StubEngine()
    feeder = Feeder(cfg, stub, pc)

    feeder.activate(conn, pid)            # initial replace-feed of the rotation
    stub.current_index = 0                # base0 is now on air
    feeder.tick(conn)                     # top the pending buffer up

    # ---- fire a spot (a legal ID) right after the on-air song ----
    ok, why = feeder.insert_spot(conn, file_path=spot_src, label="Legal ID")
    check("spot insert accepted (202)", ok)
    sid = stub.spot_id()
    check("engine queued the spot right after the play-head",
          sid is not None and stub.entries[stub.current_index + 1]["id"] == sid)
    spot_path = pc.cache_path_for(spot_src)
    check("spot precached", os.path.exists(spot_path))
    check("spot is in the feeder model", find_fed(feeder, conn, sid) is not None)

    # an hourly ID/PSA was cached long ago: no eviction grace protects it now
    age_cache(pc, spot_path)

    # ---- THE RACE: a tick whose status snapshot doesn't list the spot yet ----
    stub.hidden = {sid}
    feeder.tick(conn)

    check("spot survives a reconcile that didn't see it yet (invariant)",
          find_fed(feeder, conn, sid) is not None)
    check("spot's cached file was NOT evicted while still queued",
          os.path.exists(spot_path))

    # ---- P1 now reports the spot (observed), then it airs and ends ----
    stub.hidden = set()
    feeder.tick(conn)                     # observed as pending -> pin cleared
    check("spot still present once observed pending",
          find_fed(feeder, conn, sid) is not None)

    stub.current_index += 1               # the spot is now ON AIR (it aired!)
    check("play-head is on the spot", stub.entries[stub.current_index]["id"] == sid)
    feeder.tick(conn)
    check("spot kept while on air", find_fed(feeder, conn, sid) is not None)

    stub.current_index += 1               # spot finished, next item on air
    feeder.tick(conn)
    check("spot retired from the model after it aired (no ghost/leak)",
          find_fed(feeder, conn, sid) is None)

    # ---- a pin must EXPIRE if P1 never reports the entry at all ----
    # (P1 restarting inside the observation window would otherwise leave a
    #  ghost in 'fed' that counts toward feed_ahead and can stall feeding —
    #  a dead-air pathway, and the reason the pin is time-stamped not boolean)
    ok, why = feeder.insert_spot(conn, file_path=spot_src, label="Legal ID 2")
    check("second spot insert accepted (202)", ok)
    sid2 = feeder._load_state(conn)["fed"][0]["id"]   # insert_spot pins at [0]
    stub.hidden = {sid2}          # ...and P1 will NEVER report this one
    st = feeder._load_state(conn)
    nxt = next(e for e in st["fed"] if e["id"] == sid2)
    nxt["pinned"] = time.time() - (feeder.PIN_GRACE_SEC + 1)   # pin aged out
    feeder._save_state(conn, st)
    feeder.tick(conn)
    check("an unobserved pin expires instead of becoming a permanent ghost",
          find_fed(feeder, conn, sid2) is None)

    conn.close()
    print(f"SPOT AIRING OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
