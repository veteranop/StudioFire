"""P2 engine bridge end-to-end: real P1 (silent audio) + real feeder.

Covers: precache manifest contract, duration-target feeding with playlist
wrap, queue_version 409 re-sync, eviction after airplay, journal ingestion
with dedupe, and the web API surface.

Run: python tests/test_engine_bridge.py   (~30s)
"""
import json
import math
import os
import struct
import sys
import tempfile
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient                     # noqa: E402

from services.core import db, playlists as pl                 # noqa: E402
from services.core.app import create_app                      # noqa: E402
from services.core.engine_bridge import (                     # noqa: E402
    EngineClient, Feeder, Precache, ingest_journal)
from services.engine.control import ControlServer             # noqa: E402
from services.engine.supervisor import EngineSupervisor       # noqa: E402
from services.worker.indexer import scan                      # noqa: E402

MPV = os.path.join(ROOT, "bin", "mpv.exe")
passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def make_wav(path, seconds=3.0, freq=440):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / 8000)))
            for i in range(int(8000 * seconds))))


def wait_for(pred, timeout, what):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    print("TIMEOUT waiting for:", what)
    return False


def main():
    td = tempfile.mkdtemp(prefix="sf-bridge-")
    nas = os.path.join(td, "nas")
    os.makedirs(nas)
    tracks = []
    for i in range(4):
        p = os.path.join(nas, f"song{i}.wav")
        make_wav(p, 3.0, 300 + 80 * i)
        tracks.append(p)
    emdir = os.path.join(td, "emergency")
    os.makedirs(emdir)
    make_wav(os.path.join(emdir, "filler.wav"), 1.5, 880)
    precache_dir = os.path.join(td, "precache")

    db_path = os.path.join(td, "core.db")
    db.migrate(db_path)
    conn = db.connect(db_path)
    scan(conn, nas)  # durations into tracks table
    pid = pl.create_playlist(conn, "Main Rotation")
    for i, p in enumerate(tracks):
        pl.add_item(conn, pid, "file", p, f"Song {i}")

    journal_path = os.path.join(td, "play_journal.jsonl")
    sup = EngineSupervisor({
        "mpv_path": MPV,
        "pipe_name": "sf-bridge-" + str(os.getpid()),
        "state_path": os.path.join(td, "queue_state.json"),
        "journal_path": journal_path,
        "heartbeat_path": os.path.join(td, "heartbeat.txt"),
        "emergency_dir": emdir,
        "extra_mpv_args": ["--ao=null"],
        "watchdog_interval": 0.5,
    })
    sup.start()
    ctl = ControlServer(sup, "127.0.0.1", 0)
    ctl.start()
    base = f"http://127.0.0.1:{ctl.port}"

    try:
        # ---- precache unit behavior
        pc = Precache(precache_dir)
        cached = pc.ensure(tracks[0])
        check("precache copies into cache dir",
              cached and os.path.dirname(cached) == precache_dir
              and os.path.getsize(cached) == os.path.getsize(tracks[0]))
        check("precache is idempotent", pc.ensure(tracks[0]) == cached)
        manifest = json.load(open(os.path.join(precache_dir,
                                               "manifest.json")))
        check("manifest lists the file", cached in manifest["files"])
        check("unreadable source -> None",
              pc.ensure(os.path.join(nas, "ghost.wav")) is None)
        check("no .part litter", not [n for n in os.listdir(precache_dir)
                                      if n.endswith(".part")])
        pc.evict_except(set())
        check("evict clears file + manifest",
              not os.path.exists(cached)
              and json.load(open(os.path.join(
                  precache_dir, "manifest.json")))["files"] == {})

        # ---- feeder: activate replaces P1's queue with cached copies
        cfg = {"precache_target_minutes": 0.15,  # ~9s => ~3 tracks pending
               "db_path": db_path}
        engine = EngineClient(base)
        feeder = Feeder(cfg, engine, pc)
        ok, why = feeder.activate(conn, pid)
        check("activate feeds engine", ok)
        st = engine.status()
        check("queue populated", st["queue_len"] >= 2)
        check("playback starts from cache", wait_for(
            lambda: (engine.status()["now_playing"] or "").startswith(
                precache_dir), 10, "cache playback"))

        # ---- wrap: keep ticking while tracks burn down; cursor wraps past 4
        fed_more = False
        for _ in range(40):  # ~20s of 3s tracks
            ok, why = feeder.tick(conn)
            if why.startswith("fed"):
                fed_more = True
            time.sleep(0.5)
            if engine.status()["current_index"] >= 4:
                break
        check("feeder keeps topping up (wraps playlist)",
              fed_more and engine.status()["current_index"] >= 4)
        check("feeder stays on air (not emergency)",
              engine.status()["emergency_mode"] is False)

        # ---- eviction after airplay
        manifest = json.load(open(os.path.join(precache_dir,
                                               "manifest.json")))
        check("played files evicted (cache stays bounded)",
              len(manifest["files"]) <= 5)

        # ---- 409 re-sync: bump the version behind the feeder's back
        code, _ = engine.queue({"op": "append",
                                "queue_version":
                                    engine.status()["queue_version"] + 1,
                                "entries": []})
        check("outside mutation accepted", code == 202)
        ok, why = feeder.tick(conn)
        check("feeder survives version race (re-sync)", ok)

        # ---- journal ingestion with dedupe
        n1 = ingest_journal(conn, journal_path)
        check("journal events ingested", n1 > 0)
        n2 = ingest_journal(conn, journal_path)
        check("re-ingest is a no-op (dedupe)", n2 == 0)
        rows = conn.execute("SELECT COUNT(*) FROM play_history "
                            "WHERE event = 'track_start'").fetchone()[0]
        check("track_start rows recorded", rows >= 3)

        # ---- web API surface (feeder loop disabled; manual control)
        webtd = tempfile.mkdtemp(prefix="sf-bridge-web-")
        webcfg = {"station_name": "TestFM",
                  "db_path": db_path,
                  "secret_path": os.path.join(webtd, "secret.key"),
                  "precache_dir": precache_dir,
                  "engine_url": base,
                  "journal_path": journal_path,
                  "precache_target_minutes": 0.15,
                  "feeder_enabled": False}
        client = TestClient(create_app(webcfg), follow_redirects=False)
        client.post("/login", data={"username": "nobody", "password": "x"})
        check("engine API needs auth",
              client.get("/api/engine/status").status_code == 401)
        # user exists? DB was fresh-made by this test without users
        client.post("/setup", data={"username": "boss",
                                    "password": "longenough",
                                    "password2": "longenough"})
        r = client.get("/api/engine/status")
        check("engine status proxied",
              r.status_code == 200 and r.json()["engine_online"]
              and "queue_version" in r.json())
        r = client.post("/api/engine/op", json={"op": "pause"})
        check("pause via web API", r.status_code == 200)
        check("engine actually paused", wait_for(
            lambda: engine.status()["paused"], 5, "paused"))
        client.post("/api/engine/op", json={"op": "resume"})
        check("bad op rejected",
              client.post("/api/engine/op",
                          json={"op": "rm -rf"}).status_code == 400)
        r = client.post(f"/api/playlists/{pid}/activate")
        check("activate via web API", r.status_code == 200)
        r = client.post("/api/engine/play_next",
                        json={"path": tracks[3], "title": "Cued!"})
        check("play-next cue accepted", r.status_code == 200)
        r = client.get("/api/queue")
        check("cued track heads the queue view",
              r.json()["pending"] and
              r.json()["pending"][0]["title"] == "Cued!")
        check("play-next rejects unreadable file",
              client.post("/api/engine/play_next",
                          json={"path": tracks[3] + ".nope"})
              .status_code == 400)

        # ---- interactive queue edits: reorder / remove / cue / play-now.
        # Pause first so a real 3s track boundary can't shuffle the queue
        # out from under the deterministic assertions.
        client.post("/api/engine/op", json={"op": "pause"})
        wait_for(lambda: engine.status()["paused"], 5, "paused for edits")
        pend = client.get("/api/queue").json()["pending"]
        check("queue view exposes ids",
              len(pend) >= 3 and all(e.get("id") for e in pend))
        ids = [e["id"] for e in pend]

        client.post("/api/queue/reorder", json={"order": list(reversed(ids))})
        check("reorder rewrites pending order",
              [e["id"] for e in client.get("/api/queue").json()["pending"]]
              == list(reversed(ids)))

        victim = list(reversed(ids))[-1]
        client.post("/api/queue/remove", json={"id": victim})
        after = [e["id"] for e in client.get("/api/queue").json()["pending"]]
        check("remove drops just that item",
              victim not in after and len(after) == len(ids) - 1)

        client.post("/api/queue/cue_next", json={"id": after[-1]})
        check("cue_next jumps item to the front",
              client.get("/api/queue").json()["pending"][0]["id"] == after[-1])

        check("reorder rejects non-list order",
              client.post("/api/queue/reorder",
                          json={"order": "nope"}).status_code == 400)
        check("remove requires an id",
              client.post("/api/queue/remove", json={}).status_code == 400)

        target = client.get("/api/queue").json()["pending"][-1]
        client.post("/api/engine/op", json={"op": "resume"})
        r = client.post("/api/queue/play_now", json={"id": target["id"]})
        check("play_now accepted", r.status_code == 200)
        check("play_now cuts straight to the chosen track", wait_for(
              lambda: engine.status().get("now_title") == target["title"],
              10, "play_now cut"))

        check("activate 404 for unknown playlist",
              client.post("/api/playlists/9999/activate").status_code == 404)
    finally:
        ctl.stop()
        sup.stop()
    print(f"ENGINE BRIDGE OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
