"""First-run silence — the failover tone must not greet a fresh install.

Reported by a real user (2026-11-09, ticket 1003): "a constant tone" after
installing. The P1 failover chain's last tier is an infinite ffmpeg sine
(`av://lavfi:sine=frequency=600`) looped at full volume. On a fresh install
every other tier is empty — no queue, no emergency clips (the installer ships
none), no precache — so the very first start fell straight through to it and
the user heard a 600 Hz tone forever.

The rule these checks pin down:

  the baked-in tone is for a station that LOST its programme,
  never for a station that has not been given one yet.

Scenarios (real mpv, generated WAVs, null audio output):
  1. fresh install, nothing anywhere -> SILENT, status needs_setup, no tone
  2. same engine, first real track queued -> plays, marker written
  3. restart with the queue lost but the marker present -> tone plays
     (a configured station still gets the full failover chain)
  4. fresh install WITH an emergency clip -> plays the clip, never the tone

Run: python tests/test_failover_fresh_install.py   (silent; ~20s)
"""
import math
import os
import shutil
import struct
import sys
import tempfile
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.engine.journal import read_events                       # noqa: E402
from services.engine.queue_store import QueueStore, QueueState        # noqa: E402
from services.engine.supervisor import EngineSupervisor               # noqa: E402

MPV = os.path.join(ROOT, "bin", "mpv.exe")
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
        n = int(8000 * seconds)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / 8000)))
            for i in range(n))
        w.writeframes(frames)


def wait_for(predicate, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    print("TIMEOUT waiting for:", what)
    return False


def journal_events(cfg):
    return [e["event"] for e in read_events(cfg["journal_path"])]


def build_config(td, emergency_dir):
    return {
        "mpv_path": MPV,
        "pipe_name": "sf-fresh-" + str(os.getpid()) + "-" + os.path.basename(td),
        "state_path": os.path.join(td, "queue_state.json"),
        "journal_path": os.path.join(td, "play_journal.jsonl"),
        "heartbeat_path": os.path.join(td, "heartbeat.txt"),
        "emergency_dir": emergency_dir,
        "precache_dir": os.path.join(td, "precache"),
        "extra_mpv_args": ["--ao=null"],
        "watchdog_interval": 0.5,
    }


def entry(i, path):
    return {"id": f"t{i}", "path": path, "title": f"Track {i}", "source": "playlist"}


def main():
    # ---- 1. fresh install: nothing configured, nothing cached, no history
    td = tempfile.mkdtemp(prefix="sf-fresh-")
    emdir = os.path.join(td, "emergency")          # empty, like a real install
    os.makedirs(emdir)
    cfg = build_config(td, emdir)
    marker = os.path.join(td, "engine_has_aired")

    sup = EngineSupervisor(cfg)
    sup.start()
    try:
        check("fresh install: needs_setup reported", wait_for(
            lambda: sup.status()["needs_setup"], 10, "needs_setup"))
        check("fresh install: nothing on air (no tone)", wait_for(
            lambda: sup.status()["now_playing"] is None, 2, "silence"))
        time.sleep(3.0)                      # long enough for a tone to start
        st = sup.status()
        check("fresh install: still silent after 3s", st["now_playing"] is None)
        check("fresh install: not in emergency mode", not st["emergency_mode"])
        check("fresh install: no track_start journaled",
              "track_start" not in journal_events(cfg))
        check("fresh install: marker NOT written", not os.path.exists(marker))
        check("fresh install: journal says why", "needs_setup" in journal_events(cfg))

        # ---- 2. the first real track: plays, and the station is configured
        track = os.path.join(td, "first.wav")
        make_wav(track, seconds=3.0, freq=330)
        ok, why = sup.submit_mutation(
            {"op": "replace", "queue_version": 1, "entries": [entry(0, track)]})
        check("first track accepted", ok)
        check("first track plays", wait_for(
            lambda: sup.status()["now_playing"] == track, 10, "first.wav"))
        check("needs_setup cleared once on air", wait_for(
            lambda: not sup.status()["needs_setup"], 5, "needs_setup cleared"))
        check("marker written on first airing", wait_for(
            lambda: os.path.exists(marker), 5, "marker"))
    finally:
        sup.stop()

    # ---- 3. configured station that lost its queue: full chain, tone included
    sup2 = EngineSupervisor(cfg)             # same data dir -> marker present
    st = QueueStore(cfg["state_path"]).load()
    st.entries, st.current_index = [], -1    # queue file lost / emptied
    QueueStore(cfg["state_path"]).save(st)
    sup2.start()
    try:
        check("configured station still gets the tone when it loses audio",
              wait_for(lambda: (sup2.status()["now_playing"] or "")
                       .startswith("av://"), 10, "baked-in tone"))
        check("configured station: not flagged needs_setup",
              not sup2.status()["needs_setup"])
    finally:
        sup2.stop()

    # ---- 4. fresh install with an emergency clip: plays the clip, not a tone
    td4 = tempfile.mkdtemp(prefix="sf-fresh4-")
    emdir4 = os.path.join(td4, "emergency")
    os.makedirs(emdir4)
    make_wav(os.path.join(emdir4, "filler.wav"), seconds=1.5, freq=880)
    cfg4 = build_config(td4, emdir4)
    sup4 = EngineSupervisor(cfg4)
    sup4.start()
    try:
        check("emergency clip plays on an unconfigured install", wait_for(
            lambda: (sup4.status()["now_playing"] or "")
            .endswith("filler.wav"), 10, "filler.wav"))
    finally:
        sup4.stop()

    for path in (td, td4):
        shutil.rmtree(path, ignore_errors=True)
    print(f"\n{passed} checks passed")


if __name__ == "__main__":
    main()
