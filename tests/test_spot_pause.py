"""Regression: pausing the station must pause the spot clock.

How it goes wrong (Mark, 2026-11-19, TimeTrax 1051): a DJ comes into the
studio, plugs a laptop into the board, runs their own show, then hands the air
back to StudioFire. Pausing is how that handover works — the station is OFF
AIR for the length of the show while wall time keeps passing. The feeder tick
kept calling fire_due_spots() the whole time, and a paused engine still reports
a `now_playing` song, so every interval rule that came due was inserted into
P1's queue and marked fired. A paused queue cannot drain, so the spots piled
up; the operator went back on air to a wall of ads — a 15-minute rotation held
for an hour returned four spots back to back, on top of the ads the DJ's own
feed had already played.

Invariants under test:
  1. Nothing is inserted while the station is paused, however long it lasts.
  2. On resume each interval rule continues from where it stopped: the
     remaining time to the next spot is preserved and nothing is owed.
  3. clock / daily / weekly / once rules are NOT re-based — they are anchored
     to a wall-clock moment — and the Spots card gets a frozen countdown.
  4. The paused instant survives a core restart (it lives in feeder_state), and
     a core restart mid-show cannot turn into a burst on resume.

No real P1/mpv: a stub engine models status() / queue_version / insert_next.

Run: python tests/test_spot_pause.py   (~1s)
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
from services.core import spots as spotmod                    # noqa: E402
from services.core.engine_bridge import Feeder, Precache      # noqa: E402

passed = 0

INTERVAL_MIN = 15


def check(name, cond, extra=""):
    global passed
    if not cond:
        print("FAIL:", name, extra)
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
    """P1 stand-in for the pieces fire_due_spots()/insert_spot() touch: an
    ordered queue, a play-head, the queue_version protocol and `paused`.

    `paused` is the operator's Pause button: playback stops, and the status
    snapshot still reports the held song — exactly what fooled the feeder."""

    def __init__(self):
        self._lock = threading.Lock()
        self.entries = []
        self.version = 0
        self.current_index = -1
        self.paused = False

    def _status_locked(self):
        ci = self.current_index
        now = self.entries[ci] if 0 <= ci < len(self.entries) else None
        pending = [e["id"] for e in self.entries[ci + 1:]]
        return {"now_playing": now["path"] if now else None,
                "now_title": now.get("title") if now else None,
                "now_source": now.get("source") if now else None,
                "now_id": now["id"] if now else None,
                "duration": None, "position": None, "paused": self.paused,
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

    def spot_count(self):
        return sum(1 for e in self.entries if e.get("source") == "spot")


def build(tmp, cfg_tweak=None):
    """A feeder on a fresh DB with a base rotation on air."""
    os.makedirs(os.path.join(tmp, "nas"), exist_ok=True)
    nas = os.path.join(tmp, "nas")
    tracks = []
    for i in range(4):
        p = os.path.join(nas, f"base{i}.wav")
        make_wav(p, 0.3, 300 + 40 * i)
        tracks.append(p)
    db_path = os.path.join(tmp, "core.db")
    db.migrate(db_path)
    conn = db.connect(db_path)
    pid = pl.create_playlist(conn, "Base Rotation")
    for i, p in enumerate(tracks):
        pl.add_item(conn, pid, "file", p, f"Base {i}")
    pc = Precache(os.path.join(tmp, "precache"))
    cfg = {"precache_target_minutes": 0.02, "db_path": db_path,
           "feed_ahead_tracks": 3}
    cfg.update(cfg_tweak or {})
    stub = StubEngine()
    feeder = Feeder(cfg, stub, pc)
    feeder.activate(conn, pid)
    stub.current_index = 0          # base0 is on air
    feeder.tick(conn)
    return conn, stub, feeder, pc


def hold_clock(feeder, conn, since):
    """Put the feeder into the state a tick 1 h ago would have left: the spot
    clock held since `since`."""
    st = feeder._load_state(conn)
    st["pause_since"] = since
    feeder._save_state(conn, st)


def main():
    td = tempfile.mkdtemp(prefix="sf-spot-pause-")
    conn, stub, feeder, pc = build(td)
    spot_src = os.path.join(td, "nas", "legal_id.wav")
    make_wav(spot_src, 0.3, 990)

    now = time.time()
    held_for = 3600.0                       # a one-hour DJ show
    pause_start = now - held_for

    # the ad rotation fired 7 minutes before the show: 8 minutes of its
    # 15-minute interval were left when the operator pressed Pause
    rid = spotmod.add(conn, "dir_ads", "interval", interval_min=INTERVAL_MIN,
                      file_path=spot_src)
    remain = (INTERVAL_MIN - 7) * 60.0      # 480 s
    with conn:
        conn.execute("UPDATE spot_rules SET last_fired = ? WHERE id = ?",
                     (pause_start - (INTERVAL_MIN * 60 - remain), rid))
    rule = spotmod.get(conn, rid)
    check("set-up: the interval rule is due 8 min after the pause began",
          abs(spotmod.next_fire_epoch(rule, pause_start)
              - (pause_start + remain)) < 2)

    # ---- 1. a paused station must not fire anything --------------------
    stub.paused = True
    hold_clock(feeder, conn, pause_start)    # the show started an hour ago
    feeder.fire_due_spots(conn)              # an ordinary 5 s feeder tick
    check("nothing is inserted while the station is paused",
          stub.spot_count() == 0)
    after = spotmod.get(conn, rid)
    check("the rule is not marked fired while paused",
          abs(after["last_fired"] - rule["last_fired"]) < 1e-6)

    feeder.fire_due_spots(conn)              # ...and again, an hour in
    check("a long pause still inserts nothing (no stack builds up)",
          stub.spot_count() == 0)

    # ---- 2. resume: continue where it stopped, owe nothing --------------
    stub.paused = False
    feeder.fire_due_spots(conn)
    check("resuming does not dump the held spots (nothing owed)",
          stub.spot_count() == 0)
    resumed = spotmod.get(conn, rid)
    due_in = spotmod.next_fire_epoch(resumed, time.time()) - time.time()
    check(f"the countdown continues from where it stopped (~8 min, got "
          f"{due_in:.0f}s)", abs(due_in - remain) <= 10)
    check("nothing is due in the first minute after resume",
          not spotmod.due(resumed, time.time() + 60))
    check("...and it fires once its remaining time elapses",
          spotmod.due(resumed, time.time() + remain + 2))
    check("the held state is cleared on resume",
          feeder._load_state(conn).get("pause_since") is None)

    # ---- 3. a short pause is preserved just as precisely ----------------
    p2 = time.time() - 120.0          # the pause began two minutes ago
    remaining_before = spotmod.next_fire_epoch(spotmod.get(conn, rid),
                                               p2) - p2
    stub.paused = True
    hold_clock(feeder, conn, p2)      # ...and the feeder is holding since then
    feeder.fire_due_spots(conn)
    check("still nothing is inserted during a short pause",
          stub.spot_count() == 0)
    stub.paused = False
    feeder.fire_due_spots(conn)
    got = spotmod.next_fire_epoch(spotmod.get(conn, rid),
                                  time.time()) - time.time()
    check(f"a short pause also resumes the countdown, not the interval "
          f"(~{remaining_before:.0f}s, got {got:.0f}s)",
          abs(got - remaining_before) <= 10)
    check("still nothing was inserted by the short pause",
          stub.spot_count() == 0)

    # ---- 4. wall-clock rules are NOT re-based --------------------------
    now_min = __import__("datetime").datetime.now().minute
    far_min = (now_min + 30) % 60
    cid = spotmod.add(conn, "dir_station_ids", "clock",
                      clock_minutes=str(far_min), file_path=spot_src)
    wid = spotmod.add(conn, "dir_psas", "weekly", time_of_day="03:30",
                      days_mask=1 << 6, file_path=spot_src)   # Sunday 03:30
    oid = spotmod.add(conn, "dir_psas", "once",
                      start_at="2999-01-01T00:00", file_path=spot_src)
    before = {r: spotmod.get(conn, r)["last_fired"] for r in (cid, wid, oid)}

    stub.paused = True
    hold_clock(feeder, conn, time.time() - 900)
    feeder.fire_due_spots(conn)
    stub.paused = False
    feeder.fire_due_spots(conn)
    for r, label in ((cid, "clock"), (wid, "weekly"), (oid, "once")):
        after_lf = spotmod.get(conn, r)["last_fired"]
        check(f"a {label} rule is not re-based by a pause",
              before[r] == after_lf)
    check("the ad rotation was re-based by the same pause instead",
          spotmod.get(conn, rid)["last_fired"] != resumed["last_fired"])

    # ---- 5. the held clock SURVIVES a core restart ---------------------
    stub.paused = True
    feeder.fire_due_spots(conn)              # tick observes the pause
    held_at = feeder._load_state(conn)["pause_since"]
    feeder2 = Feeder({"precache_target_minutes": 0.02,
                      "db_path": feeder.cfg["db_path"],
                      "feed_ahead_tracks": 3}, stub, pc)   # a fresh process
    feeder2.fire_due_spots(conn)
    check("a core restart mid-show keeps the spot clock held",
          feeder2._load_state(conn).get("pause_since") == held_at)
    check("...and the restarted feeder still inserts nothing",
          stub.spot_count() == 0)

    # ---- 6. what the Spots card is told -------------------------------
    shown = feeder2.spot_clock_held(conn, stub.status())
    check("the UI is told when the clock stopped", shown == held_at)
    ui_rule = spotmod.get(conn, rid)
    nxt_at_held = spotmod.next_fire_epoch(ui_rule, shown)
    # held_remaining in GET /api/spots is exactly this subtraction
    expected_remaining = spotmod.next_fire_epoch(ui_rule, held_at) - held_at
    check("the countdown shown while held is the real remaining time",
          abs((nxt_at_held - shown) - expected_remaining) <= 1)
    stub.paused = False
    feeder2.fire_due_spots(conn)
    check("once playback resumes the UI gets a live countdown again",
          feeder2.spot_clock_held(conn, stub.status()) is None)

    # ---- 7. the shift helper itself ------------------------------------
    n = spotmod.shift_interval_baselines(conn, 0.4)
    check("a sub-second blip is not written", n == 0)

    # ---- 8. end to end: what GET /api/spots hands the dashboard ---------
    # A real HTTP stub stands in for P1's /status, so this exercises the whole
    # chain — engine paused -> feeder holds the clock -> the API's frozen
    # countdown — which is exactly what the Spots card renders.
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from fastapi.testclient import TestClient
    from services.core.app import create_app

    class _StatusStub(BaseHTTPRequestHandler):
        paused = True

        def do_GET(self):
            if self.path != "/status":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({
                "now_playing": spot_src, "paused": _StatusStub.paused,
                "emergency_mode": False, "queue_version": 0}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), _StatusStub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    webtd = tempfile.mkdtemp(prefix="sf-pause-api-")
    cfg = {"station_name": "PauseTest", "db_path": feeder.cfg["db_path"],
           "secret_path": os.path.join(webtd, "secret.key"),
           "precache_dir": os.path.join(webtd, "precache"),
           "engine_url": f"http://127.0.0.1:{port}",
           "journal_path": os.path.join(webtd, "play_journal.jsonl"),
           "feeder_enabled": False}
    os.makedirs(cfg["precache_dir"], exist_ok=True)

    # the feeder observed the pause a minute ago
    api_held = time.time() - 60
    st = feeder._load_state(conn)
    st["pause_since"] = api_held
    feeder._save_state(conn, st)

    client = TestClient(create_app(cfg), follow_redirects=False)
    client.post("/setup", data={"username": "boss", "password": "longenough",
                                "password2": "longenough"})
    d = client.get("/api/spots").json()
    check("API reports the held clock to the dashboard", d.get("paused") is True,
          str(d.get("paused")))
    check("API says when the clock stopped", abs(d.get("paused_since") - api_held) < 1)
    held_rows = [r for r in d["rules"] if r.get("held_remaining") is not None]
    check("every timed rule carries a frozen countdown", len(held_rows) >= 1,
          str(len(held_rows)))
    api_rule = next(r for r in d["rules"] if r["id"] == rid)
    check("the held countdown is seconds left at the pause, not since",
          api_rule["held_remaining"] > 60)   # 1 minute already held, ~8 left
    _StatusStub.paused = False
    d2 = client.get("/api/spots").json()
    check("API drops the held flag once playback resumes",
          d2.get("paused") is False and d2.get("paused_since") is None)
    check("...and every rule is back to a live countdown",
          all(r.get("held_remaining") is None for r in d2["rules"]))
    srv.shutdown()

    conn.close()

    print(f"SPOT PAUSE OK ({passed} checks)")


if __name__ == "__main__":
    main()
