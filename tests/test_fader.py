"""Two-deck crossfade tests (John's feedback: the next song must start
rising the moment the old one starts falling — no dip; spots/IDs/PSAs are
never faded).

  1. fade_volume() curve.
  2. Real mpv (null audio), both decks sampled every ~50ms:
     song -> song   overlap: both audible at once, old falls, new rises
     song -> spot   spot starts under the fade at FULL volume, stays full
     spot -> song   song starts when the spot ends, at full (no dip)
     filler         full volume
  3. crossfade_sec = 0: back to back, never two decks at once, all full.
  4. Operator: skip mid-crossfade is a hard cut; stop-after suppresses the
     crossfade and holds the next song paused.

Run: python -m tests.test_fader   (silent; ~60s)
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
XF = 2.0     # crossfade seconds used by the real-mpv checks


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
    # equal power: at the halfway point gain = sin(45°) = 0.707, which on
    # mpv's cubic volume scale is 100 · 0.707^(1/3) ≈ 89
    check("curve: halfway through the fade-in is equal-power (~89)",
          round(fade_volume(1.0, 200, 2, 4)) == 89)
    check("curve: full volume mid-song", fade_volume(100, 200, 2, 4) == 100)
    check("curve: halfway through the fade-out is equal-power (~89)",
          round(fade_volume(198, 200, 2, 4)) == 89)
    # constant loudness: outgoing + incoming POWER stays ~1 through a fade
    # (gain = (vol/100)^3 on mpv's scale; power = gain^2)
    worst = min(((fade_volume(t, 4, 0, 4) / 100) ** 6
                 + (fade_volume(t, None, 4, 0) / 100) ** 6)
                for t in [i / 20 * 4 for i in range(21)])
    check("curve: a crossfade keeps constant loudness (no dip)",
          worst > 0.97)
    check("curve: silent at the very end", fade_volume(200, 200, 2, 4) == 0)
    check("curve: unknown position -> full (never guess toward silence)",
          fade_volume(None, 200, 2, 4) == 100)
    check("curve: unknown duration -> no fade-out",
          fade_volume(100, None, 2, 4) == 100)


def sample(sup, stop, out):
    """Per tick: [(deck, path, pos, volume, paused)] for every deck that has
    a file, read from MPV ITSELF (path re-read after; changed = dropped)."""
    while not stop.is_set():
        tick = []
        for d in list(sup._decks):
            c = d.client
            if c is None:
                continue
            try:
                path = c.get_property("path", timeout=0.5)
                pos = c.get_property("time-pos", timeout=0.5)
                vol = c.get_property("volume", timeout=0.5)
                paused = c.get_property("pause", timeout=0.5)
                if path and c.get_property("path", timeout=0.5) == path:
                    tick.append((d.name, path, pos, vol, paused))
            except Exception:
                pass
        out.append((time.monotonic(), tick))
        time.sleep(0.05)


def audible(tick, name):
    """(pos, vol) of file `name` if it's playing (not paused) in this tick."""
    for _d, path, pos, vol, paused in tick:
        if path and path.endswith(name) and not paused and pos is not None:
            return pos, vol
    return None


def run(cfg_extra, entries_spec, td, until, secs=60, during=None):
    """Play `entries_spec` [(filename, source)] on a fresh engine and return
    the sample log. `until(sup)` = done; `during(sup)` runs in parallel."""
    emdir = os.path.join(td, "emergency")
    os.makedirs(emdir, exist_ok=True)
    cfg = build_config(td, emdir)
    cfg["pipe_name"] += "-xf" + str(abs(hash(td)) % 10000)
    cfg.update(cfg_extra)
    sup = EngineSupervisor(cfg)
    sup.start()
    samples, stop = [], threading.Event()
    t = threading.Thread(target=sample, args=(sup, stop, samples), daemon=True)
    try:
        t.start()
        entries = [{"id": f"e{i}", "path": os.path.join(td, n),
                    "title": n, "source": src}
                   for i, (n, src) in enumerate(entries_spec)]
        ok, _ = sup.submit_mutation({"op": "replace", "queue_version": 1,
                                     "entries": entries})
        check("queue accepted", ok)
        if during:
            during(sup)
        wait_for(lambda: until(sup), secs, "scenario end")
        time.sleep(0.5)
    finally:
        stop.set()
        t.join(2)
        sup.stop()
    return samples, sup


def crossfade_checks():
    td = tempfile.mkdtemp(prefix="sf-xf-")
    for name, secs, f in (("song1.wav", 7.0, 440), ("song2.wav", 7.0, 550),
                          ("spot.wav", 4.0, 660), ("song3.wav", 6.0, 330)):
        make_wav(os.path.join(td, name), seconds=secs, freq=f)
    make_wav(os.path.join(td, "emergency", "filler.wav"), 3.0, 880) \
        if os.makedirs(os.path.join(td, "emergency"), exist_ok=True) is None \
        else None
    samples, _ = run(
        {"crossfade_sec": XF},
        [("song1.wav", "playlist"), ("song2.wav", "playlist"),
         ("spot.wav", "spot"), ("song3.wav", "manual")], td,
        until=lambda s: s.status()["emergency_mode"]
        and s.status()["now_playing"] and "filler" in s.status()["now_playing"])

    both_12 = [(audible(t, "song1.wav"), audible(t, "song2.wav"))
               for _, t in samples]
    overlap = [(a, b) for a, b in both_12 if a and b]
    check("song -> song OVERLAPS: both songs audible at the same time",
          len(overlap) >= 10)
    # compare the first and last THIRD of the overlap rather than single end
    # samples: under load a sample read can fail and drop a deck from a tick
    third = max(1, len(overlap) // 3)

    def avg(xs):
        return sum(xs) / len(xs)
    old_early = avg([a[1] for a, _ in overlap[:third]])
    old_late = avg([a[1] for a, _ in overlap[-third:]])
    new_early = avg([b[1] for _, b in overlap[:third]])
    new_late = avg([b[1] for _, b in overlap[-third:]])
    # equal-power keeps the outgoing song near full most of the way and drops
    # it fast at the end (100 -> 97 -> 89 -> 73 -> 0; it reaches 0 half a
    # second before end-of-file), so compare thirds and the overall span
    check("...the old song falls through the overlap",
          old_late < old_early - 15
          and min(a[1] for a, _ in overlap) <= 60)
    check("...while the new song rises from silence to near full",
          new_early < new_late - 15 and overlap[0][1][1] <= 10
          and max(b[1] for _, b in overlap) >= 90)
    # equal-power: the midpoint is ~89 each; a straight-line fade would be
    # 50 each (≈ -15 dB combined, an audible dip)
    check("...and there's no dip: the louder of the two stays up",
          min(max(a[1], b[1]) for a, b in overlap) >= 70)
    s2 = [audible(t, "song2.wav") for _, t in samples]
    check("new song reaches full volume after the crossfade",
          all(v == 100 for pos, v in (x for x in s2 if x) if XF + 0.4 < pos < 4.5))

    spot = [audible(t, "spot.wav") for _, t in samples]
    spot = [x for x in spot if x]
    check("song -> spot: the spot is at FULL volume start to finish",
          len(spot) > 20 and all(v == 100 for _, v in spot))
    under = [(audible(t, "song2.wav"), audible(t, "spot.wav"))
             for _, t in samples]
    check("...starting under the song's fade-out (they overlap)",
          any(a and b for a, b in under))

    s3 = [(audible(t, "song3.wav"), audible(t, "spot.wav"))
          for _, t in samples]
    # (at most the one hand-off sample — a real overlap spans many)
    check("spot -> song: the song waits for the spot to finish",
          sum(1 for a, b in s3 if a and b) <= 1)
    s3v = [a for a, _ in s3 if a]
    check("...and starts at FULL volume (no fade-in dip after a spot)",
          s3v and all(v == 100 for pos, v in s3v if pos < 4.0))
    fil = [audible(t, "filler.wav") for _, t in samples]
    fil = [x for x in fil if x]
    check("emergency filler at full volume", fil
          and all(v == 100 for _, v in fil))


def no_crossfade_checks():
    td = tempfile.mkdtemp(prefix="sf-xf0-")
    os.makedirs(os.path.join(td, "emergency"))
    make_wav(os.path.join(td, "a.wav"), 3.0, 440)
    make_wav(os.path.join(td, "b.wav"), 3.0, 550)
    samples, _ = run({"crossfade_sec": 0},
                     [("a.wav", "playlist"), ("b.wav", "playlist")], td,
                     until=lambda s: s.status()["emergency_mode"]
                     and s.status()["now_id"] is None
                     and s.status()["current_index"] >= 1, secs=30)
    ticks = [t for _, t in samples]
    # a sample reads the decks one after another (ms apart), so the exact
    # hand-off instant can show both once; a real overlap lasts many samples
    both = sum(1 for t in ticks if audible(t, "a.wav") and audible(t, "b.wav"))
    check("crossfade 0: no overlap (at most the one hand-off sample)",
          both <= 1)
    check("crossfade 0: everything at full volume",
          all(v == 100 for t in ticks for _d, p, pos, v, paused in t
              if not paused and p and p.endswith(("a.wav", "b.wav"))))
    check("crossfade 0: both played",
          any(audible(t, "a.wav") for t in ticks)
          and any(audible(t, "b.wav") for t in ticks))


def operator_checks():
    td = tempfile.mkdtemp(prefix="sf-xfop-")
    os.makedirs(os.path.join(td, "emergency"))
    for n in ("s1.wav", "s2.wav", "s3.wav"):
        make_wav(os.path.join(td, n), 6.0, 440)

    def skip_mid_crossfade(sup):
        wait_for(lambda: sum(1 for d in sup._decks if d.role == "fading"),
                 20, "crossfade starts")
        ok, _ = sup.submit_command("skip")
        check("skip accepted mid-crossfade", ok)

    samples, sup = run({"crossfade_sec": XF},
                       [("s1.wav", "playlist"), ("s2.wav", "playlist"),
                        ("s3.wav", "playlist")], td,
                       until=lambda s: s.status()["now_playing"]
                       and s.status()["now_playing"].endswith("s3.wav"),
                       secs=30, during=skip_mid_crossfade)
    time.sleep(0)
    last = samples[-1][1]
    check("skip mid-crossfade is a hard cut: only the next song is left",
          audible(last, "s3.wav") and not audible(last, "s1.wav")
          and not audible(last, "s2.wav"))
    s3 = [audible(t, "s3.wav") for _, t in samples]
    check("...and it starts at full volume",
          all(v == 100 for pos, v in (x for x in s3 if x)))

    td2 = tempfile.mkdtemp(prefix="sf-xfsa-")
    os.makedirs(os.path.join(td2, "emergency"))
    for n in ("s1.wav", "s2.wav"):
        make_wav(os.path.join(td2, n), 5.0, 440)

    def arm_stop_after(sup):
        ok, _ = sup.submit_command("stop_after")
        check("stop-after armed", ok)

    samples, sup = run({"crossfade_sec": XF},
                       [("s1.wav", "playlist"), ("s2.wav", "playlist")], td2,
                       until=lambda s: s.status()["paused"]
                       and (s.status()["now_playing"] or "").endswith("s2.wav"),
                       secs=20, during=arm_stop_after)
    ticks = [t for _, t in samples]
    check("stop-after: no crossfade (s2 never audible while s1 plays)",
          not any(audible(t, "s1.wav") and audible(t, "s2.wav") for t in ticks))
    check("stop-after: next song held paused, not playing",
          not any(audible(t, "s2.wav") for t in ticks))


def meter_checks():
    """On Air level meter: real mpv, a tone of known level (make_wav peaks at
    12000/32768 = -8.7 dBFS)."""
    td = tempfile.mkdtemp(prefix="sf-vu-")
    os.makedirs(os.path.join(td, "emergency"))
    make_wav(os.path.join(td, "tone.wav"), 8.0, 440)
    make_wav(os.path.join(td, "tone2.wav"), 8.0, 550)
    emdir = os.path.join(td, "emergency")
    for meter_on in (True, False):
        cfg = build_config(td, emdir)
        cfg["pipe_name"] += "-vu" + str(int(meter_on))
        for k in ("state_path", "journal_path", "heartbeat_path"):
            cfg[k] += f".{int(meter_on)}"   # a fresh engine each run
        cfg["crossfade_sec"] = 3.0
        cfg["level_meter"] = meter_on
        sup = EngineSupervisor(cfg)
        sup.start()
        try:
            sup.submit_mutation({"op": "replace", "queue_version": 1,
                                 "entries": [
                {"id": "a", "path": os.path.join(td, "tone.wav"),
                 "title": "a", "source": "playlist"},
                {"id": "b", "path": os.path.join(td, "tone2.wav"),
                 "title": "b", "source": "playlist"}]})
            wait_for(lambda: (sup.status()["position"] or 0) > 0.5, 10, "play")
            if not meter_on:
                check("meter off: engine reports it unavailable",
                      sup.levels()["enabled"] is False)
                check("meter off: playback unaffected",
                      (sup.status()["now_playing"] or "").endswith("tone.wav"))
                continue
            check("meter on: available", sup.levels()["enabled"] is True)
            wait_for(lambda: sup.levels()["channels"][0]["peak"] is not None,
                     5, "meter reading")
            peaks = []
            for _ in range(8):
                ch = sup.levels()["channels"]
                if ch[0]["peak"] is not None:
                    peaks.append((ch[0]["peak"], ch[1]["peak"]))
                time.sleep(0.1)
            check("meter reads the tone's real level (~ -8.7 dBFS peak)",
                  peaks and all(abs(left - (-8.7)) < 1.5 for left, _ in peaks))
            check("a mono file shows on both channels",
                  all(left == right for left, right in peaks))
            # during the crossfade the OLD tone falls; with both decks the
            # program level stays up, then settles on the new tone
            wait_for(lambda: any(d.role == "fading" for d in sup._decks),
                     15, "crossfade")
            check("mid-crossfade: two decks feed the meter",
                  wait_for(lambda: sup.levels()["decks"] == 2, 3, "2 decks"))
            ok, _ = sup.submit_command("pause")
            check("pausing drops the meter to silence", ok and wait_for(
                lambda: sup.levels()["channels"][0]["peak"] is None
                and sup.levels()["paused"], 3, "silence"))
        finally:
            sup.stop()


def main():
    curve_checks()
    meter_checks()
    crossfade_checks()
    no_crossfade_checks()
    operator_checks()
    print(f"FADER OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
