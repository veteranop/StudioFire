"""Torture harness — §10.7 torture matrix + soak gate for Phase 0.

Pass criterion (PLAN.md §10.7): zero audible silence longer than 2 seconds.
An AirMonitor thread samples engine status ~4x/sec and records the longest
window with no playback progress (position frozen / nothing playing while
not paused). Any gap > GAP_LIMIT fails the run.

Scenarios (on top of tests/test_supervisor_bench.py's seven):
  T1. mutation flood      — 4 threads x 60 mixed valid/stale/garbage mutations
  T2. corrupt file mid-play — next queued track overwritten with garbage bytes
  T3. cache exhaustion    — every pending file deleted mid-play -> emergency
  T4. restart storm       — mpv killed 5x in a row, recovers every time
  T5. recovery from storm — fresh queue accepted and plays after the abuse

Run:
  python tests/torture.py            -> one pass of T1-T5 (~2 min, silent)
  python tests/torture.py soak 72    -> 72h soak: continuous playback with
                                        random fault injection; report line
                                        appended to logs/torture_report.jsonl
                                        every 5 min. Ctrl+C = early summary.
"""
import json
import math
import os
import random
import struct
import sys
import tempfile
import threading
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.engine.journal import read_events          # noqa: E402
from services.engine.supervisor import EngineSupervisor  # noqa: E402

MPV = os.path.join(ROOT, "bin", "mpv.exe")
GAP_LIMIT = 2.0          # seconds — the §10.7 dead-air ceiling
passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def make_wav(path, seconds=2.0, freq=440):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / 8000)))
            for i in range(int(8000 * seconds))))


def wait_for(pred, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    print("TIMEOUT waiting for:", what)
    return False


class AirMonitor(threading.Thread):
    """Watches for playback progress; records the longest dead window."""

    def __init__(self, sup):
        super().__init__(name="air-monitor", daemon=True)
        self.sup = sup
        self.max_gap = 0.0
        self.gap_events = []          # (utc, gap_seconds)
        self._stop = threading.Event()
        self._last_sig = None
        self._last_progress = None    # monotonic ts of last observed progress

    def run(self):
        while not self._stop.wait(0.25):
            st = self.sup.status()
            now = time.monotonic()
            sig = (st["now_playing"], st["position"])
            if st["paused"]:
                # intentional silence — reset the clock, don't count it
                self._last_progress = now
                self._last_sig = sig
                continue
            if sig != self._last_sig and st["now_playing"] is not None:
                if self._last_progress is not None:
                    gap = now - self._last_progress
                    if gap > self.max_gap:
                        self.max_gap = gap
                    if gap > GAP_LIMIT:
                        self.gap_events.append((time.time(), round(gap, 3)))
                        print(f"!!! DEAD AIR {gap:.2f}s "
                              f"(now_playing={st['now_playing']})")
                self._last_progress = now
                self._last_sig = sig
            elif self._last_progress is None:
                self._last_progress = now  # arm on first sample

    def current_gap(self):
        if self._last_progress is None:
            return 0.0
        return time.monotonic() - self._last_progress

    def stop(self):
        self._stop.set()
        self.join(2)


def build_config(td, emergency_dir, tag):
    return {
        "mpv_path": MPV,
        "pipe_name": f"sf-torture-{tag}-{os.getpid()}",
        "state_path": os.path.join(td, "queue_state.json"),
        "journal_path": os.path.join(td, "play_journal.jsonl"),
        "heartbeat_path": os.path.join(td, "heartbeat.txt"),
        "emergency_dir": emergency_dir,
        "extra_mpv_args": ["--ao=null"],
        "watchdog_interval": 0.5,
    }


def entry(i, path):
    return {"id": f"t{i}", "path": path, "title": f"Track {i}",
            "source": "playlist"}


def setup_env(tag, n_tracks=6, track_secs=3.0):
    td = tempfile.mkdtemp(prefix=f"sf-torture-{tag}-")
    tracks = []
    for i in range(n_tracks):
        p = os.path.join(td, f"track{i}.wav")
        make_wav(p, seconds=track_secs, freq=300 + 60 * i)
        tracks.append(p)
    emdir = os.path.join(td, "emergency")
    os.makedirs(emdir)
    make_wav(os.path.join(emdir, "filler.wav"), seconds=1.5, freq=880)
    return td, tracks, emdir


# ------------------------------------------------------------------ scenarios

def t1_mutation_flood(sup, tracks):
    """Hammer the mutation path from 4 threads: valid appends, stale
    versions, and garbage ops. Engine must survive and keep playing."""
    errors = []
    accepted = [0]
    lock = threading.Lock()

    def flood(tid):
        for i in range(60):
            r = random.random()
            try:
                if r < 0.4:   # stale version — must be rejected, not crash
                    sup.submit_mutation({"op": "append", "queue_version": 1,
                                         "entries": []})
                elif r < 0.6:  # garbage op
                    sup.submit_mutation({"op": "detonate",
                                         "queue_version": 999999})
                elif r < 0.8:  # garbage entries
                    sup.submit_mutation({
                        "op": "append",
                        "queue_version": sup.status()["queue_version"] + 1,
                        "entries": [{"bogus": True}]})
                else:          # racing valid append
                    ok, _ = sup.submit_mutation({
                        "op": "append",
                        "queue_version": sup.status()["queue_version"] + 1,
                        "entries": [entry(1000 + tid * 100 + i,
                                          tracks[i % len(tracks)])]})
                    if ok:
                        with lock:
                            accepted[0] += 1
            except Exception as exc:  # noqa: BLE001 — harness must record all
                errors.append(f"thread{tid}#{i}: {exc!r}")

    threads = [threading.Thread(target=flood, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    check("T1 flood: no exceptions escaped", not errors)
    check("T1 flood: some valid appends won the race", accepted[0] > 0)
    st = sup.status()
    check("T1 flood: mpv alive and playing",
          st["mpv_alive"] and st["now_playing"] is not None)


def t2_corrupt_mid_play(sup, td):
    """Overwrite the NEXT queued track with garbage while current plays.
    Engine must skip past it without a >2s gap."""
    good = os.path.join(td, "t2-good.wav")
    bad = os.path.join(td, "t2-bad.wav")
    tail = os.path.join(td, "t2-tail.wav")
    make_wav(good, 4.0, 500)
    make_wav(bad, 4.0, 520)
    make_wav(tail, 3.0, 540)
    v = sup.status()["queue_version"] + 1
    ok, _ = sup.submit_mutation({"op": "replace", "queue_version": v,
                                 "entries": [entry(0, good), entry(1, bad),
                                             entry(2, tail)]})
    check("T2 corrupt: queue accepted", ok)
    check("T2 corrupt: good track starts", wait_for(
        lambda: sup.status()["now_playing"] == good, 10, "t2 good"))
    # corrupt the next track while the first is on air
    with open(bad, "wb") as f:
        f.write(os.urandom(8192))
    check("T2 corrupt: engine reaches tail track past the corrupt one",
          wait_for(lambda: sup.status()["now_playing"] == tail, 15, "t2 tail"))


def t3_cache_exhaustion(sup, td):
    """Delete every pending file mid-play — precisely what a dead NAS looks
    like once the local cache drains. Must land in emergency, on air."""
    files = []
    for i in range(3):
        p = os.path.join(td, f"t3-{i}.wav")
        make_wav(p, 4.0, 600 + 40 * i)
        files.append(p)
    v = sup.status()["queue_version"] + 1
    ok, _ = sup.submit_mutation({"op": "replace", "queue_version": v,
                                 "entries": [entry(i, p)
                                             for i, p in enumerate(files)]})
    check("T3 exhaustion: queue accepted", ok)
    check("T3 exhaustion: first track starts", wait_for(
        lambda: sup.status()["now_playing"] == files[0], 10, "t3 first"))
    for p in files[1:]:
        os.remove(p)
    check("T3 exhaustion: falls to emergency, still on air", wait_for(
        lambda: sup.status()["emergency_mode"]
        and sup.status()["now_playing"] is not None, 15, "t3 emergency"))


def t4_restart_storm(sup, cfg):
    """Kill mpv 5 times in a row. Watchdog must bring it back every time."""
    for n in range(5):
        client = sup._client  # test-only reach into internals
        if client is not None and client._proc is not None:
            try:
                client._proc.kill()
            except OSError:
                pass
        check(f"T4 storm: recovery #{n + 1}", wait_for(
            lambda: sup.status()["mpv_alive"]
            and sup.status()["now_playing"] is not None, 12, f"storm {n}"))
        time.sleep(2.0)
    restarts = [e for e in read_events(cfg["journal_path"])
                if e["event"] == "mpv_restart"]
    check("T4 storm: journal recorded the restarts", len(restarts) >= 5)


def t5_recover_from_storm(sup, td):
    """After all the abuse: a normal queue must still just work."""
    p = os.path.join(td, "t5-clean.wav")
    make_wav(p, 3.0, 700)
    v = sup.status()["queue_version"] + 1
    ok, _ = sup.submit_mutation({"op": "replace", "queue_version": v,
                                 "entries": [entry(0, p)]})
    check("T5 recovery: queue accepted", ok)
    check("T5 recovery: clean track plays, emergency cleared", wait_for(
        lambda: sup.status()["now_playing"] == p
        and not sup.status()["emergency_mode"], 12, "t5 clean"))


def run_matrix():
    td, tracks, emdir = setup_env("matrix")
    cfg = build_config(td, emdir, "matrix")
    sup = EngineSupervisor(cfg)
    sup.start()
    ok, _ = sup.submit_mutation({"op": "replace", "queue_version": 1,
                                 "entries": [entry(i, p)
                                             for i, p in enumerate(tracks)]})
    check("matrix: initial queue accepted", ok)
    check("matrix: playback starts", wait_for(
        lambda: sup.status()["now_playing"] is not None, 10, "start"))
    mon = AirMonitor(sup)
    mon.start()
    try:
        t1_mutation_flood(sup, tracks)
        t2_corrupt_mid_play(sup, td)
        t3_cache_exhaustion(sup, td)
        t4_restart_storm(sup, cfg)
        t5_recover_from_storm(sup, td)
    finally:
        mon.stop()
        sup.stop()
    print(f"\nlongest no-progress window: {mon.max_gap:.2f}s "
          f"(limit {GAP_LIMIT:.1f}s)")
    check("DEAD-AIR GATE: no gap exceeded the limit", not mon.gap_events)
    print(f"TORTURE MATRIX OK ({passed} checks)")


# ----------------------------------------------------------------------- soak

FAULTS = ("kill_mpv", "corrupt_next", "delete_next", "stale_flood", "none")


def run_soak(hours):
    td, tracks, emdir = setup_env("soak", n_tracks=8, track_secs=6.0)
    cfg = build_config(td, emdir, "soak")
    report_path = os.path.join(ROOT, "logs", "torture_report.jsonl")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    sup = EngineSupervisor(cfg)
    sup.start()
    mon = AirMonitor(sup)
    faults = {f: 0 for f in FAULTS}
    started = time.monotonic()
    deadline = started + hours * 3600
    next_report = started + 300

    def refill():
        """Keep ~6 tracks pending, like P2's pre-cache feeder will."""
        st = sup.status()
        pending = st["queue_len"] - (st["current_index"] + 1)
        if pending < 6:
            batch = [entry(random.randrange(10**9), random.choice(tracks))
                     for _ in range(6 - pending)]
            sup.submit_mutation({"op": "append",
                                 "queue_version": st["queue_version"] + 1,
                                 "entries": batch})

    def inject(fault):
        if fault == "kill_mpv":
            client = sup._client
            if client is not None and client._proc is not None:
                try:
                    client._proc.kill()
                except OSError:
                    pass
        elif fault in ("corrupt_next", "delete_next"):
            # sabotage a random source wav, then regenerate it afterwards
            victim = random.choice(tracks)
            if fault == "corrupt_next":
                with open(victim, "wb") as f:
                    f.write(os.urandom(4096))
            else:
                try:
                    os.remove(victim)
                except OSError:
                    pass
            time.sleep(3)
            make_wav(victim, 6.0, random.randrange(300, 900))
        elif fault == "stale_flood":
            for _ in range(30):
                sup.submit_mutation({"op": "append", "queue_version": 1,
                                     "entries": []})

    def write_report(final=False):
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "elapsed_h": round((time.monotonic() - started) / 3600, 3),
               "max_gap_s": round(mon.max_gap, 3),
               "gap_violations": len(mon.gap_events),
               "faults": dict(faults), "final": final}
        with open(report_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        print(("FINAL " if final else "") + json.dumps(rec))

    refill()
    if not wait_for(lambda: sup.status()["now_playing"] is not None,
                    15, "soak start"):
        sup.stop()
        return 1
    mon.start()
    print(f"soak running for {hours}h — report: {report_path}")
    try:
        while time.monotonic() < deadline:
            refill()
            if random.random() < 0.05:  # ~1 fault per ~100s of 5s ticks
                fault = random.choice(FAULTS)
                faults[fault] += 1
                if fault != "none":
                    print(f"[fault] {fault}")
                inject(fault)
            if time.monotonic() >= next_report:
                write_report()
                next_report += 300
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nsoak interrupted — writing final report")
    finally:
        mon.stop()
        sup.stop()
        write_report(final=True)
    if mon.gap_events:
        print(f"SOAK FAIL: {len(mon.gap_events)} dead-air events "
              f"(worst {mon.max_gap:.2f}s)")
        return 1
    print(f"SOAK PASS: zero gaps over {GAP_LIMIT}s "
          f"(worst {mon.max_gap:.2f}s)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "soak":
        hours = float(sys.argv[2]) if len(sys.argv) > 2 else 72.0
        sys.exit(run_soak(hours))
    run_matrix()
    sys.exit(0)
