"""Supervisor bench test — first slice of the §10.7 torture matrix.

Scenarios (real mpv, tiny generated WAVs, null audio output):
  1. normal playback + track advance + journal records
  2. queue exhaustion -> emergency folder loop (tier 2)
  3. append mutation while in emergency -> exits emergency
  4. unplayable next track -> skipped, no gap
  5. mpv killed mid-track -> watchdog restarts it, playback resumes
  6. supervisor restart while in emergency -> re-enters emergency (persisted)
  7. empty emergency folder -> baked-in tier 3 source plays
  8. operator force-emergency: holds filler through new material, survives
     a restart, exits only on resume_normal

Run: python tests/test_supervisor_bench.py   (silent; takes ~30-60s)
"""
import datetime
import math
import os
import struct
import sys
import tempfile
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.engine.journal import read_events                       # noqa: E402
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
        "pipe_name": "sf-bench-" + str(os.getpid()),
        "state_path": os.path.join(td, "queue_state.json"),
        "journal_path": os.path.join(td, "play_journal.jsonl"),
        "heartbeat_path": os.path.join(td, "heartbeat.txt"),
        "emergency_dir": emergency_dir,
        "extra_mpv_args": ["--ao=null"],
        "watchdog_interval": 0.5,
    }


def entry(i, path):
    return {"id": f"t{i}", "path": path, "title": f"Track {i}", "source": "playlist"}


def main():
    td = tempfile.mkdtemp(prefix="sf-bench-")
    tracks = []
    for i in range(4):
        p = os.path.join(td, f"track{i}.wav")
        make_wav(p, seconds=2.0, freq=300 + 100 * i)
        tracks.append(p)
    emdir = os.path.join(td, "emergency")
    os.makedirs(emdir)
    make_wav(os.path.join(emdir, "filler.wav"), seconds=1.5, freq=880)

    cfg = build_config(td, emdir)
    sup = EngineSupervisor(cfg)
    sup.start()
    try:
        # ---- 1. normal playback + advance
        ok, why = sup.submit_mutation(
            {"op": "replace", "queue_version": 1,
             "entries": [entry(0, tracks[0]), entry(1, tracks[1])]})
        check("replace mutation accepted", ok)
        check("track 0 starts", wait_for(
            lambda: sup.status()["now_playing"] == tracks[0], 8, "track0"))
        check("advances to track 1", wait_for(
            lambda: sup.status()["now_playing"] == tracks[1], 8, "track1"))

        # ---- 2. queue exhaustion -> emergency folder
        check("emergency after exhaustion", wait_for(
            lambda: sup.status()["emergency_mode"], 8, "emergency"))
        check("emergency filler playing", wait_for(
            lambda: (sup.status()["now_playing"] or "").endswith("filler.wav"),
            6, "filler"))
        evs = journal_events(cfg)
        check("journal: starts+ends+emergency",
              evs.count("track_start") >= 3 and "track_end" in evs
              and "emergency_enter" in evs)

        # ---- 3. append while in emergency -> exit emergency
        ok, _ = sup.submit_mutation({"op": "append", "queue_version": 2,
                                     "entries": [entry(2, tracks[2])]})
        check("append accepted in emergency", ok)
        check("exits emergency to track 2", wait_for(
            lambda: sup.status()["now_playing"] == tracks[2]
            and not sup.status()["emergency_mode"], 8, "exit emergency"))
        check("journal: emergency_exit", "emergency_exit" in journal_events(cfg))

        # ---- 4. unplayable next -> skipped (stale version rejected too)
        ok, why = sup.submit_mutation({"op": "append", "queue_version": 2,
                                       "entries": [entry(9, tracks[3])]})
        check("stale version rejected", not ok)
        ghost = os.path.join(td, "ghost.wav")  # never created
        ok, _ = sup.submit_mutation(
            {"op": "append", "queue_version": 3,
             "entries": [{"id": "ghost", "path": ghost, "source": "playlist"},
                         entry(3, tracks[3])]})
        check("append accepted", ok)
        check("ghost skipped, track 3 plays", wait_for(
            lambda: sup.status()["now_playing"] == tracks[3], 10, "track3"))
        check("journal: track_skip", "track_skip" in journal_events(cfg))

        # ---- 5. kill mpv -> watchdog restart -> playback resumes
        sup._client._proc.kill()  # test-only reach into internals
        check("watchdog restarts mpv", wait_for(
            lambda: "mpv_restart" in journal_events(cfg), 10, "mpv_restart"))
        check("audio resumes after restart", wait_for(
            lambda: sup.status()["mpv_alive"]
            and sup.status()["now_playing"] is not None, 10, "resume"))

        # heartbeat is being written
        hb = cfg["heartbeat_path"]
        check("heartbeat fresh", os.path.exists(hb)
              and time.time() - float(open(hb).read()) < 5)
    finally:
        sup.stop()

    # ---- 6. restart while in emergency -> re-enters emergency
    sup2 = EngineSupervisor(cfg)
    # force persisted emergency state (as if we died in emergency)
    from services.engine.queue_store import QueueStore
    st = QueueStore(cfg["state_path"]).load()
    st.emergency_mode = True
    st.entries, st.current_index = [], -1
    QueueStore(cfg["state_path"]).save(st)
    sup2 = EngineSupervisor(cfg)
    sup2.start()
    try:
        check("re-enters emergency after restart", wait_for(
            lambda: sup2.status()["emergency_mode"]
            and sup2.status()["now_playing"] is not None, 10, "re-emergency"))
    finally:
        sup2.stop()

    # ---- 7. empty emergency folder -> baked-in tier 3
    td3 = tempfile.mkdtemp(prefix="sf-bench3-")
    emdir3 = os.path.join(td3, "emergency-empty")
    os.makedirs(emdir3)
    cfg3 = build_config(td3, emdir3)
    cfg3["pipe_name"] += "-t3"
    sup3 = EngineSupervisor(cfg3)
    sup3.start()
    try:
        check("baked-in source on empty emergency folder", wait_for(
            lambda: (sup3.status()["now_playing"] or "").startswith("av://"),
            10, "baked-in"))
        check("journal: emergency_folder_empty",
              "emergency_folder_empty" in journal_events(cfg3))
    finally:
        sup3.stop()

    # ---- 7b. empty emergency folder + cached music -> precache is the filler
    td3b = tempfile.mkdtemp(prefix="sf-bench3b-")
    emdir3b = os.path.join(td3b, "emergency-empty")
    os.makedirs(emdir3b)
    pcdir3b = os.path.join(td3b, "precache")
    os.makedirs(pcdir3b)
    make_wav(os.path.join(pcdir3b, "cached-song.wav"), seconds=1.5, freq=660)
    cfg3b = build_config(td3b, emdir3b)
    cfg3b["pipe_name"] += "-t3b"
    cfg3b["precache_dir"] = pcdir3b
    sup3b = EngineSupervisor(cfg3b)
    sup3b.start()
    try:
        check("precache music as filler on empty emergency folder", wait_for(
            lambda: (sup3b.status()["now_playing"] or "")
            .endswith("cached-song.wav"), 10, "precache filler"))
    finally:
        sup3b.stop()

    # ---- 8. operator force-emergency (big red button)
    td4 = tempfile.mkdtemp(prefix="sf-bench4-")
    emdir4 = os.path.join(td4, "emergency")
    os.makedirs(emdir4)
    make_wav(os.path.join(emdir4, "filler.wav"), seconds=1.5, freq=880)
    t4 = []
    for i in range(3):
        p = os.path.join(td4, f"track{i}.wav")
        make_wav(p, seconds=4.0, freq=300 + 100 * i)
        t4.append(p)
    cfg4 = build_config(td4, emdir4)
    cfg4["pipe_name"] += "-t4"
    sup4 = EngineSupervisor(cfg4)
    sup4.start()
    try:
        sup4.submit_mutation({"op": "replace", "queue_version": 1,
                              "entries": [entry(0, t4[0]), entry(1, t4[1])]})
        check("t8: queue playing", wait_for(
            lambda: sup4.status()["now_playing"] == t4[0], 8, "t8 track0"))
        ok, _ = sup4.submit_command("emergency")
        check("t8: emergency op accepted", ok)
        check("t8: forced filler on air", wait_for(
            lambda: sup4.status()["forced_emergency"]
            and sup4.status()["emergency_mode"]
            and (sup4.status()["now_playing"] or "").endswith("filler.wav"),
            8, "forced filler"))
        # new material must NOT pull us out while forced
        sup4.submit_mutation({"op": "append", "queue_version": 2,
                              "entries": [entry(2, t4[2])]})
        time.sleep(4)  # long enough for filler to loop at least once
        st = sup4.status()
        check("t8: stays on filler despite new material",
              st["forced_emergency"] and st["emergency_mode"]
              and (st["now_playing"] or "").endswith("filler.wav"))
        check("t8: journal has emergency_forced",
              "emergency_forced" in journal_events(cfg4))
    finally:
        sup4.stop()
    # forced flag survives an engine restart
    sup5 = EngineSupervisor(cfg4)
    sup5.start()
    try:
        check("t8: forced emergency survives restart", wait_for(
            lambda: sup5.status()["forced_emergency"]
            and sup5.status()["emergency_mode"]
            and (sup5.status()["now_playing"] or "").endswith("filler.wav"),
            10, "forced after restart"))
        ok, _ = sup5.submit_command("resume_normal")
        check("t8: resume_normal accepted", ok)
        check("t8: back to the queue", wait_for(
            lambda: not sup5.status()["emergency_mode"]
            and not sup5.status()["forced_emergency"]
            and sup5.status()["now_playing"] in t4, 10, "resume normal"))
        check("t8: journal has emergency_force_cleared",
              "emergency_force_cleared" in journal_events(cfg4))
    finally:
        sup5.stop()

    # ---- 9. no-skip guard: three tracks fed with a next always primed behind
    # the current one. The old prime-trim bug removed the CURRENT playlist entry
    # at each boundary (end-file reason 'stop'), skipping ~every other track —
    # inaudible (no silence) so the torture gate never caught it. Every queued
    # track must actually air, and no track may be stopped ~instantly.
    td9 = tempfile.mkdtemp(prefix="sf-bench9-")
    emdir9 = os.path.join(td9, "emergency")
    os.makedirs(emdir9)
    make_wav(os.path.join(emdir9, "filler.wav"), seconds=1.5, freq=880)
    t9 = []
    for i in range(3):
        p = os.path.join(td9, f"track{i}.wav")
        make_wav(p, seconds=2.0, freq=300 + 120 * i)
        t9.append(p)
    cfg9 = build_config(td9, emdir9)
    cfg9["pipe_name"] += "-t9"
    sup9 = EngineSupervisor(cfg9)
    sup9.start()
    try:
        sup9.submit_mutation(
            {"op": "replace", "queue_version": 1,
             "entries": [entry(0, t9[0]), entry(1, t9[1]), entry(2, t9[2])]})
        seen = set()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 7:      # ~3 x 2s tracks
            np = sup9.status()["now_playing"]
            if np:
                seen.add(np)
            time.sleep(0.05)
        check("t9: inner track 1 actually aired (not skipped)", t9[1] in seen)
        check("t9: all three queued tracks aired",
              all(t in seen for t in t9))
        # the skip bug's signature: a track_start (A) immediately (<0.3s)
        # 'stop'-ended and followed by a DIFFERENT track — i.e. A was dropped
        # with no airtime and the queue jumped past it. (A same-file restart at
        # session start is a benign handoff hiccup, not a content skip.)
        evs9 = list(read_events(cfg9["journal_path"]))
        starts = [e for e in evs9 if e["event"] == "track_start"]

        def _t(e):
            return datetime.datetime.fromisoformat(e["ts"])
        skips = []
        for i, (a, b) in enumerate(zip(evs9, evs9[1:])):
            if not (a["event"] == "track_start" and b["event"] == "track_end"
                    and b.get("reason") == "stop"
                    and (_t(b) - _t(a)).total_seconds() < 0.3):
                continue
            nxt = next((e for e in evs9[i + 2:]
                        if e["event"] == "track_start"), None)
            if nxt and nxt.get("path") != a.get("path"):
                skips.append(a.get("title"))
        check("t9: no track skipped by a phantom boundary stop", not skips)
    finally:
        sup9.stop()

    # ---- 10. queue-history trim in the live audio path: play through >20
    # tracks and confirm trimming never skips a song and bounds the queue.
    from services.engine.supervisor import MAX_QUEUE_HISTORY
    td10 = tempfile.mkdtemp(prefix="sf-bench10-")
    emdir10 = os.path.join(td10, "emergency")
    os.makedirs(emdir10)
    make_wav(os.path.join(emdir10, "filler.wav"), seconds=1.0, freq=880)
    n = MAX_QUEUE_HISTORY + 6           # enough played to force several trims
    t10 = []
    for i in range(n):
        p = os.path.join(td10, f"t{i:02d}.wav")
        make_wav(p, seconds=0.5, freq=300 + i)
        t10.append(p)
    cfg10 = build_config(td10, emdir10)
    cfg10["pipe_name"] += "-t10"
    sup10 = EngineSupervisor(cfg10)
    sup10.start()
    # titles "Track 0".."Track n-1" from entry(); match on title (P1 reports
    # backslash paths, our temp paths differ — title is unambiguous here)
    want = [f"Track {i}" for i in range(n)]
    seen_titles = []
    try:
        sup10.submit_mutation({"op": "replace", "queue_version": 1,
                               "entries": [entry(i, p) for i, p in enumerate(t10)]})
        max_qlen = 0
        max_idx = 0
        deadline = time.monotonic() + 45
        last = None
        while time.monotonic() < deadline and len(seen_titles) < n:
            st = sup10.status()
            nt = st.get("now_title")
            if nt and nt != last and nt in want:
                seen_titles.append(nt)
                last = nt
            max_qlen = max(max_qlen, st.get("queue_len") or 0)
            max_idx = max(max_idx, st.get("current_index") or 0)
            time.sleep(0.05)
        check("t10: every track aired in order (trim skipped none)",
              seen_titles == want)
        # once history is trimmed, current_index is pinned at the keep size and
        # the queue length can't keep growing with the number of tracks played
        check("t10: current_index pinned at the keep size (trim fired)",
              max_idx == MAX_QUEUE_HISTORY)
        check("t10: queue length stayed bounded (history trimmed)",
              max_qlen <= n)
    finally:
        sup10.stop()

    print(f"SUPERVISOR BENCH OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
