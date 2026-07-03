"""End-to-end: full P1 stack (supervisor + control) driven over HTTP,
exactly the way P2 will drive it.

Run: python tests/test_control.py   (silent audio; ~20s)
"""
import json
import math
import os
import struct
import sys
import tempfile
import time
import urllib.request
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.engine.control import ControlServer        # noqa: E402
from services.engine.supervisor import EngineSupervisor  # noqa: E402

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
        w.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / 8000)))
            for i in range(int(8000 * seconds))))


def http(method, url, body=None):
    req = urllib.request.Request(url, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for(pred, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    print("TIMEOUT:", what)
    return False


def main():
    td = tempfile.mkdtemp(prefix="sf-ctl-")
    t0 = os.path.join(td, "a.wav")
    t1 = os.path.join(td, "b.wav")
    make_wav(t0, 3.0, 400)
    make_wav(t1, 3.0, 500)
    emdir = os.path.join(td, "em")
    os.makedirs(emdir)
    make_wav(os.path.join(emdir, "filler.wav"), 1.5, 880)

    sup = EngineSupervisor({
        "mpv_path": MPV,
        "pipe_name": "sf-ctl-" + str(os.getpid()),
        "state_path": os.path.join(td, "queue_state.json"),
        "journal_path": os.path.join(td, "play_journal.jsonl"),
        "heartbeat_path": os.path.join(td, "heartbeat.txt"),
        "emergency_dir": emdir,
        "extra_mpv_args": ["--ao=null"],
        "watchdog_interval": 0.5,
    })
    sup.start()
    ctl = ControlServer(sup, "127.0.0.1", 0)  # ephemeral port
    ctl.start()
    base = f"http://127.0.0.1:{ctl.port}"
    try:
        code, body = http("GET", base + "/health")
        check("health", code == 200 and body["ok"])

        code, body = http("POST", base + "/queue", {
            "op": "replace", "queue_version": 1,
            "entries": [{"id": "a", "path": t0, "title": "A", "source": "playlist"},
                        {"id": "b", "path": t1, "title": "B", "source": "playlist"}]})
        check("queue replace 202", code == 202 and body["accepted"])

        check("playing track A", wait_for(
            lambda: http("GET", base + "/status")[1]["now_playing"] == t0,
            8, "track A"))

        code, body = http("POST", base + "/queue", {
            "op": "append", "queue_version": 1, "entries": []})
        check("stale version -> 409 + status echo",
              code == 409 and "status" in body)

        code, body = http("POST", base + "/op", {"op": "pause"})
        check("pause accepted", code == 200 and body["accepted"])
        check("status shows paused", wait_for(
            lambda: http("GET", base + "/status")[1]["paused"], 3, "paused"))
        code, body = http("POST", base + "/op", {"op": "resume"})
        check("resume accepted", code == 200)

        code, body = http("POST", base + "/op", {"op": "skip"})
        check("skip accepted", code == 200)
        check("skip lands on track B", wait_for(
            lambda: http("GET", base + "/status")[1]["now_playing"] == t1,
            8, "track B"))

        code, body = http("POST", base + "/op", {"op": "selfdestruct"})
        check("bad op -> 400", code == 400)
        code, body = http("POST", base + "/queue", {"op": "explode",
                                                    "queue_version": 99})
        check("bad queue op -> 400", code == 400)
        code, _ = http("GET", base + "/nope")
        check("404 for unknown path", code == 404)

        status = http("GET", base + "/status")[1]
        check("status carries queue fields",
              status["queue_version"] == 1 and status["queue_len"] == 2)
    finally:
        ctl.stop()
        sup.stop()
    print(f"CONTROL OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
