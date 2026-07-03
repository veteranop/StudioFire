"""Phase 0 smoke test #2: real playback through MpvClient.

Proves, with actual audio out the default device:
1. load + play a real MP3, position advances
2. second file queued via loadfile append -> gapless advance on skip
3. event stream delivers start-file/end-file (what journal.py will consume)
4. killing mpv is detected via ping()/is_running() (watchdog primitive)

Run: python tests/playback_smoke.py   (you will hear ~8s of audio)
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.engine.mpv_ipc import MpvClient, MpvError  # noqa: E402

MPV = os.path.join(ROOT, "bin", "mpv.exe")
AUDIO = os.path.join(ROOT, "media", "Audio")
TRACKS = [os.path.join(AUDIO, "test-one.mp3"), os.path.join(AUDIO, "test2.mp3")]

events = []


def on_event(msg):
    if msg.get("event") in ("start-file", "end-file", "playback-restart"):
        events.append(msg.get("event"))


def wait_for_pos(client, timeout=5.0):
    """Poll until time-pos exists (mpv exposes it only once audio starts)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pos = client.get_property("time-pos")
            if pos is not None:
                return pos
        except MpvError:  # 'property unavailable' while file is loading
            pass
        time.sleep(0.1)
    raise AssertionError("audio never started within %.1fs" % timeout)


def main() -> int:
    for t in TRACKS:
        if not os.path.isfile(t):
            print("FAIL: missing test track:", t)
            return 1

    client = MpvClient(MPV, "studiofire-playback-smoke", event_callback=on_event)
    client.start()
    try:
        # 1. play first track, verify position advances
        client.command("loadfile", TRACKS[0], "replace")
        p1 = wait_for_pos(client)
        time.sleep(2.0)
        p2 = client.get_property("time-pos")
        print(f"track1 pos: {p1:.2f} -> {p2:.2f}")
        assert p2 > p1, "position not advancing"
        print("now playing:", os.path.basename(client.get_property("path")))

        # 2. queue second track, skip to it (simulates track advance)
        client.command("loadfile", TRACKS[1], "append")
        client.command("playlist-next")
        pos = wait_for_pos(client)
        now = client.get_property("path")
        print(f"after advance: {os.path.basename(now)} pos={pos:.2f}")
        assert now == TRACKS[1], "did not advance to track 2"

        # 3. events observed
        print("events seen:", events)
        assert "start-file" in events, "no start-file events"
        assert "end-file" in events, "no end-file event on track change"

        # 4. watchdog primitive: kill mpv, detect death
        assert client.ping(), "ping failed while alive"
        client._proc.kill()  # simulate crash (test-only reach into internals)
        time.sleep(0.5)
        dead_detected = not client.is_running()
        ping_result = client.ping()
        print(f"after kill: is_running={not dead_detected}, ping={ping_result}")
        assert dead_detected and not ping_result, "death not detected"

        print("PLAYBACK SMOKE OK")
        return 0
    finally:
        client.stop()


if __name__ == "__main__":
    sys.exit(main())
