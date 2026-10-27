"""Stereo channel-integrity regression test (TimeTrax #895, KDPI).

John reported "some tracks are not playing certain parts of the song — IE
lead guitar parts", cured by switching the receiver from stereo to mono. That
is the classic signature of ONE stereo channel being lost between playout and
the listener: summing to mono puts the hard-panned part back on the surviving
channel. This test settles the CODE-side question empirically:

    Can StudioFire drop or attenuate one channel of a stereo programme?

Two independent proofs, both against real bin/mpv.exe:

 1. FILE CAPTURE (one-shot subprocess, the engine's exact AUDIO flags —
    --no-config --gapless-audio=yes --keep-open=no --volume): a hard-panned
    source is rendered to a PCM/WAV file and read back PER CHANNEL. We assert
    the tone stays on the channel it was authored on, the opposite channel
    stays silent, and the result is IDENTICAL with and without the On Air
    meter's astats filter (so the one filter our code inserts is provably
    pass-through). mpv's IPC/UI flags (--idle, --input-ipc-server,
    --prefetch-playlist) are control-plane, not DSP, so they are omitted here;
    the live IPC `af set` path is exercised separately in proof 2.

 2. LIVE METER READ (the real production path): a hard-panned file is played
    through services.engine.mpv_ipc.MpvClient with the supervisor's
    METER_FILTER attached via the exact `af set` IPC command, and the
    per-channel level is read back from `af-metadata/meter` exactly as
    supervisor._read_meter does. The authored channel must read a real level
    and the silent channel must read silence — i.e. our inserted filter sees
    BOTH channels and drops neither.

Extra cases: mono-then-stereo in ONE gapless/prefetch instance (the only
plausible in-code "shared chain state" mechanism), and a volume ramp
(crossfade) must scale both channels equally.

Finally the test PROVES it can fail: it renders through a real
channel-dropping pan filter and asserts the per-channel checker flags the
loss. If that self-check stops failing, the whole test is worthless.

No ffmpeg dependency; the hard-panned WAV is synthesised with the stdlib.
Run: python -m tests.test_channel_integrity        (~15s)
"""
from __future__ import annotations

import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.engine.mpv_ipc import MpvClient                   # noqa: E402
from services.engine.supervisor import METER_FILTER, _db        # noqa: E402

MPV = os.path.join(ROOT, "bin", "mpv.exe")
RATE = 8000
AMP = 12000           # 12000/32768 => ~-8.7 dBFS peak (matches make_wav)
passed = 0

# the engine's AUDIO-affecting flags (services/engine/mpv_ipc.py::start). The
# rest of mpv's command line there is control-plane (idle/IPC/prefetch) and
# does not touch the sample path; proof 2 drives the live IPC path instead.
ENGINE_AUDIO_FLAGS = ["--no-config", "--no-video", "--no-terminal",
                      "--keep-open=no", "--gapless-audio=yes"]
# the meter's filter graph as it reaches mpv, minus the "@meter:" label mpv
# uses for the runtime `af set`; derived from the real constant so this test
# tracks any change to it.
METER_AF = METER_FILTER.split(":", 1)[1]


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


# --------------------------------------------------------------- test material

def make_stereo_wav(path, seconds=2.0, left_freq=0, right_freq=0):
    """16-bit stereo WAV; freq == 0 => that channel is digital silence, so a
    hard-panned source (tone one side, silence the other) needs no encoder."""
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        frames = bytearray()
        for i in range(int(RATE * seconds)):
            left = int(AMP * math.sin(2 * math.pi * left_freq * i / RATE)) \
                if left_freq else 0
            right = int(AMP * math.sin(2 * math.pi * right_freq * i / RATE)) \
                if right_freq else 0
            frames += struct.pack("<hh", left, right)
        w.writeframes(bytes(frames))


def make_mono_wav(path, seconds=2.0, freq=440):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(
            struct.pack("<h", int(AMP * math.sin(2 * math.pi * freq * i / RATE)))
            for i in range(int(RATE * seconds))))


# --------------------------------------------------------------- capture read

def channel_rms(path, start_frac=0.0, end_frac=1.0):
    """Per-channel RMS (0..~1.0 full scale) over a fraction of the capture.
    Returns (nchannels, [rms_ch0, rms_ch1, ...])."""
    with wave.open(path, "rb") as w:
        nch = w.getnchannels()
        sw = w.getsampwidth()
        total = w.getnframes()
        w.setpos(int(total * start_frac))
        want = int(total * end_frac) - int(total * start_frac)
        raw = w.readframes(max(0, want))
    if sw != 2:
        raise AssertionError(f"expected 16-bit PCM, got sampwidth={sw}")
    samples = struct.unpack("<%dh" % (len(raw) // 2), raw) if raw else []
    sums = [0.0] * nch
    count = 0
    for i in range(0, len(samples) - nch + 1, nch):
        for c in range(nch):
            s = samples[i + c] / 32768.0
            sums[c] += s * s
        count += 1
    rms = [math.sqrt(s / count) if count else 0.0 for s in sums]
    return nch, rms


def render_file(srcs, out_wav, af=None, volume=100):
    """Render one or more sources to a PCM/WAV file through a one-shot mpv
    launched with the engine's exact audio flags. `af` is an mpv --af graph
    (e.g. the meter's astats, or a channel-dropping pan). Multiple srcs play
    gapless, back to back, into one continuous capture."""
    if isinstance(srcs, str):
        srcs = [srcs]
    args = [MPV, *ENGINE_AUDIO_FLAGS, "--volume=" + str(volume),
            "--ao=pcm", "--ao-pcm-file=" + out_wav, "--ao-pcm-waveheader=yes"]
    if af:
        args.append("--af=" + af)
    args += srcs
    subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=30,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return out_wav


# --------------------------------------------------------------------- proof 1

def file_capture_checks(td):
    """A hard-panned tone must survive the real engine audio chain, per
    channel, identically with and without the On Air meter's astats filter."""
    right = os.path.join(td, "right_panned.wav")   # tone RIGHT, silent LEFT
    left = os.path.join(td, "left_panned.wav")     # tone LEFT, silent RIGHT
    make_stereo_wav(right, 2.0, left_freq=0, right_freq=550)
    make_stereo_wav(left, 2.0, left_freq=440, right_freq=0)

    results = {}
    for tag, src, tone_ch, silent_ch, af in (
            ("right-no-meter", right, 1, 0, None),
            ("right-meter", right, 1, 0, METER_AF),
            ("left-no-meter", left, 0, 1, None),
            ("left-meter", left, 0, 1, METER_AF)):
        out = os.path.join(td, "cap_" + tag + ".wav")
        render_file(src, out, af=af)
        nch, rms = channel_rms(out)
        results[tag] = rms
        check(f"[{tag}] capture is stereo with audio", nch == 2 and sum(rms) > 0)
        check(f"[{tag}] authored channel carries the tone (rms {rms[tone_ch]:.3f})",
              rms[tone_ch] > 0.10)
        check(f"[{tag}] opposite channel stays silent (rms {rms[silent_ch]:.4f})",
              rms[silent_ch] < 0.01)
        check(f"[{tag}] no cross-channel bleed (tone >> silent)",
              rms[tone_ch] > rms[silent_ch] * 20)

    # the single highest-value assertion: WITH and WITHOUT the meter filter the
    # per-channel energy is identical to within measurement noise -> the one
    # filter our code inserts into a live chain is provably pass-through.
    for side, a, b in (("right", "right-no-meter", "right-meter"),
                       ("left", "left-no-meter", "left-meter")):
        r0, r1 = results[a], results[b]
        delta = max(abs(r0[0] - r1[0]), abs(r0[1] - r1[1]))
        check(f"METER_FILTER is pass-through on the {side} channel "
              f"(max per-channel RMS delta {delta:.4f})", delta < 0.01)


# --------------------------------------------------------------------- proof 2

def live_meter_read_check(td):
    """Drive the REAL production path: MpvClient + `af set METER_FILTER`
    (exactly as supervisor._start_deck), then read `af-metadata/meter` and
    parse it exactly as supervisor._read_meter. The authored channel must read
    a real level; the silent channel must read silence."""
    right = os.path.join(td, "right_panned.wav")
    if not os.path.isfile(right):
        make_stereo_wav(right, 2.0, left_freq=0, right_freq=550)
    client = MpvClient(MPV, f"sf-chmeter-{os.getpid()}",
                       extra_args=["--ao=null"])   # measure only, no device
    client.start()
    try:
        client.set_property("pause", True)
        client.command("af", "set", METER_FILTER)   # supervisor's exact call
        client.command("loadfile", right, "replace")
        client.set_property("loop-file", "inf")      # keep it running to sample
        client.set_property("pause", False)
        deadline = time.monotonic() + 8
        ch1 = ch2 = None
        while time.monotonic() < deadline:
            time.sleep(0.15)
            md = client.get_property("af-metadata/meter", timeout=0.5)
            if not isinstance(md, dict):
                continue
            p1 = _db(md.get("lavfi.astats.1.Peak_level"))   # left
            p2 = _db(md.get("lavfi.astats.2.Peak_level"))   # right
            if p2 is not None:          # right channel (the tone) has levelled
                ch1, ch2 = p1, p2
                break
        check("live meter: the engine's af-metadata exposes per-channel levels",
              ch2 is not None)
        check(f"live meter: authored (right) channel reads a real level "
              f"({ch2} dBFS)", ch2 is not None and ch2 > -20.0)
        # pure-silence astats reports -inf -> _db returns None (= silence)
        check(f"live meter: silent (left) channel reads silence ({ch1})",
              ch1 is None or ch1 < -60.0)
    finally:
        client.stop()


# --------------------------------------------------------------------- extras

def gapless_mono_then_stereo(td):
    """The one plausible in-CODE mechanism for 'some tracks lose parts': a
    mono file ahead of a stereo file in ONE gapless/prefetch instance could
    pin the chain to mono and strip the stereo file's panned content. Prove
    what actually happens to the hard-panned tone."""
    mono = os.path.join(td, "mono.wav")
    stereo = os.path.join(td, "after_mono_right.wav")
    make_mono_wav(mono, 1.0, freq=330)
    make_stereo_wav(stereo, 2.0, left_freq=0, right_freq=550)
    out = os.path.join(td, "cap_gapless.wav")
    render_file([mono, stereo], out)         # positional playlist, gapless

    nch, _ = channel_rms(out)
    # second segment (skip the 1s mono head of the ~3s capture) is the stereo
    nch2, rms2 = channel_rms(out, start_frac=1.0 / 3.0 + 0.1, end_frac=0.98)
    print(f"      gapless capture: {nch} channel(s); "
          f"stereo-segment rms={['%.3f' % x for x in rms2]}")
    if nch2 == 1:
        # mono-first pinned the chain to one channel -> the stereo file was
        # DOWNMIXED (summed), not channel-dropped: the right tone is summed in,
        # so no CONTENT is lost (it reappears exactly like John's mono fix)
        check("gapless: right-panned tone survives even when the chain "
              "collapsed to mono (summed, not dropped)", rms2[0] > 0.05)
    else:
        check("gapless: stereo file keeps its right panning after a mono file "
              f"(right rms {rms2[1]:.3f})", rms2[1] > 0.10)
        check("gapless: left stays silent after the mono file "
              f"(left rms {rms2[0]:.4f})", rms2[0] < 0.02)


def volume_ramp_balance(td):
    """A crossfade ramps mpv's `volume` (cubic, whole-signal). Confirm it
    attenuates both channels equally -> the L/R ratio is unchanged across
    volumes (an equal-power fade must not favour one channel)."""
    both = os.path.join(td, "both.wav")
    make_stereo_wav(both, 1.5, left_freq=440, right_freq=550)
    ratios = {}
    for vol in (100, 50):
        out = os.path.join(td, f"cap_vol{vol}.wav")
        render_file(both, out, volume=vol)
        _, rms = channel_rms(out)
        ratios[vol] = rms[0] / rms[1] if rms[1] else float("inf")
        print(f"      volume {vol}: L/R rms = {rms[0]:.3f}/{rms[1]:.3f} "
              f"ratio {ratios[vol]:.4f}")
    check("volume ramp keeps L/R balance (ratio unchanged across volumes)",
          abs(ratios[100] - ratios[50]) < 0.05)


def prove_it_can_fail(td):
    """Render the right-panned source through a real channel-dropping pan
    (right output := left input, so the hard-right tone is discarded) and
    assert the per-channel checker DETECTS the loss. If this stops failing,
    every green assertion above is meaningless."""
    right = os.path.join(td, "right_panned.wav")
    if not os.path.isfile(right):
        make_stereo_wav(right, 2.0, left_freq=0, right_freq=550)
    out = os.path.join(td, "cap_dropped.wav")
    # pan is a libavfilter filter -> wrap in lavfi; c1:=c0 discards right input
    render_file(right, out, af="lavfi=[pan=stereo|c0=c0|c1=c0]")
    nch, rms = channel_rms(out)
    print(f"      dropped-channel capture: {nch}ch "
          f"rms={['%.4f' % x for x in rms]}")
    # the real assertion 'rms[1] > 0.10' would FAIL here -> the test has teeth
    tone_lost = not (rms[1] > 0.10)
    check("drop-detection: a real dropped channel makes the integrity "
          "assertion FAIL (the test has teeth)", tone_lost)


def main():
    if not os.path.isfile(MPV):
        print("SKIP: bin/mpv.exe missing — cannot run real-audio test")
        return 0
    td = tempfile.mkdtemp(prefix="sf-chint-")
    file_capture_checks(td)
    live_meter_read_check(td)
    gapless_mono_then_stereo(td)
    volume_ramp_balance(td)
    prove_it_can_fail(td)
    print(f"CHANNEL INTEGRITY OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
