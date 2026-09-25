"""Song fade-in / fade-out tests (John's feedback: fade between songs, but
spots/IDs/PSAs play at full volume the whole way through).

  1. fade_volume() curve: ramps in, holds full, ramps out; never guesses
     toward silence when position/duration are unknown.
  2. Real mpv (null audio): song -> spot -> song. Samples mpv's actual volume
     property while it plays and checks the song fades in/out, the spot is
     at full volume from its first instant to its last, and the station ends
     on full volume after the queue drains to emergency filler.

Run: python -m tests.test_fader   (silent; takes ~30s)
"""
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.engine.supervisor import (EngineSupervisor,          # noqa: E402
                                        fade_volume)
from tests.test_supervisor_bench import (build_config, make_wav,  # noqa: E402
                                         wait_for)

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def curve_checks():
    check("curve: silent at the very start of a fade-in",
          fade_volume(0.0, 200, 2, 4) == 0)
    check("curve: halfway through the fade-in",
          fade_volume(1.0, 200, 2, 4) == 50)
    check("curve: full volume mid-song",
          fade_volume(100, 200, 2, 4) == 100)
    check("curve: halfway through the fade-out",
          fade_volume(198, 200, 2, 4) == 50)
    check("curve: silent at the very end",
          fade_volume(200, 200, 2, 4) == 0)
    check("curve: unknown position -> full (never guess toward silence)",
          fade_volume(None, 200, 2, 4) == 100)
    check("curve: unknown duration -> no fade-out",
          fade_volume(100, None, 2, 4) == 100)
    check("curve: fades off -> always full",
          fade_volume(0.0, 200, 0, 0) == 100
          and fade_volume(199.9, 200, 0, 0) == 100)
    check("curve: a song shorter than both fades peaks below full, no error",
          0 < fade_volume(1.0, 2.5, 2, 4) < 100)


def sample(sup, stop, out):
    """(now_source, path, time-pos, volume) every ~50ms from real mpv. The
    file is taken from MPV ITSELF (read before and after the other reads,
    sample dropped if it changed): the engine's status lags mpv by a moment
    at every track change, which would pin the next file's first instant on
    the previous file."""
    while not stop.is_set():
        c = sup._client
        try:
            st = sup.status()
            path = c.get_property("path", timeout=0.5)
            pos = c.get_property("time-pos", timeout=0.5)
            vol = c.get_property("volume", timeout=0.5)
            if c.get_property("path", timeout=0.5) == path:
                out.append((st["now_source"], path, pos, vol))
        except Exception:
            pass
        time.sleep(0.05)


def real_mpv_checks():
    td = tempfile.mkdtemp(prefix="sf-fade-")
    song1 = os.path.join(td, "song1.wav")
    spot = os.path.join(td, "spot.wav")
    song2 = os.path.join(td, "song2.wav")
    make_wav(song1, seconds=6.0, freq=440)
    make_wav(spot, seconds=4.0, freq=660)
    make_wav(song2, seconds=6.0, freq=550)
    emdir = os.path.join(td, "emergency")
    os.makedirs(emdir)
    make_wav(os.path.join(emdir, "filler.wav"), seconds=3.0, freq=880)

    cfg = build_config(td, emdir)
    cfg["pipe_name"] += "-fade"
    cfg["fade_in_sec"] = 1.0
    cfg["fade_out_sec"] = 1.5
    sup = EngineSupervisor(cfg)
    sup.start()
    samples, stop = [], threading.Event()
    sampler = threading.Thread(target=sample, args=(sup, stop, samples),
                               daemon=True)
    try:
        sampler.start()
        ok, _ = sup.submit_mutation({"op": "replace", "queue_version": 1,
                                     "entries": [
            {"id": "s1", "path": song1, "title": "Song 1", "source": "playlist"},
            {"id": "sp", "path": spot, "title": "Spot", "source": "spot"},
            {"id": "s2", "path": song2, "title": "Song 2", "source": "manual"},
        ]})
        check("queue accepted", ok)
        # (the engine sits in emergency until its first queue arrives, so
        # wait for the LAST song before waiting for filler)
        check("reaches the last song", wait_for(
            lambda: sup.status()["now_playing"] == song2, 30, "song2"))
        check("plays through to emergency filler", wait_for(
            lambda: sup.status()["emergency_mode"], 30, "queue drained"))
        time.sleep(1.0)
    finally:
        stop.set()
        sampler.join(2)
        sup.stop()

    def of(path):
        return [(pos, vol) for _, p, pos, vol in samples
                if p == path and pos is not None and vol is not None]

    s1, sp, s2 = of(song1), of(spot), of(song2)
    check("sampled all three files", len(s1) > 20 and len(sp) > 20
          and len(s2) > 20)
    # timing slack: one fader tick (0.1s) + IPC round trips
    check("song fades in (quiet in its first 0.3s)",
          all(v <= 60 for pos, v in s1 if pos < 0.3))
    check("song is at full volume mid-song",
          all(v == 100 for pos, v in s1 if 1.5 < pos < 4.0))
    check("song fades out (quiet in its last 0.4s)",
          all(v <= 45 for pos, v in s1 if pos > 5.6))
    check("SPOT is at full volume from start to finish",
          all(v == 100 for _, v in sp))
    check("a manual 'play next' song fades too",
          any(v < 100 for pos, v in s2 if pos < 0.5)
          and all(v == 100 for pos, v in s2 if 1.5 < pos < 4.0))
    filler = [v for src, _, _, v in samples
              if src == "emergency" and v is not None]
    check("emergency filler is at full volume", filler
          and all(v == 100 for v in filler[3:]))


def main():
    curve_checks()
    real_mpv_checks()
    print(f"FADER OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
