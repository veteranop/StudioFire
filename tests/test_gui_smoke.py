"""GUI smoke: pages render, library search works, queue/tiles APIs answer
sanely with the engine offline (P2 must degrade, never 500).

Run: python tests/test_gui_smoke.py
"""
import math
import os
import struct
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient             # noqa: E402

from services.core import db, playlists as pl         # noqa: E402
from services.core.app import create_app              # noqa: E402
from services.worker.indexer import scan              # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def make_wav(path, seconds=1.0):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * i / 8000)))
            for i in range(int(8000 * seconds))))


def main():
    td = tempfile.mkdtemp(prefix="sf-gui-")
    nas = os.path.join(td, "nas")
    os.makedirs(nas)
    make_wav(os.path.join(nas, "Highway Song.wav"))
    make_wav(os.path.join(nas, "Blue Morning.wav"))

    cfg = {"station_name": "TestFM",
           "db_path": os.path.join(td, "core.db"),
           "secret_path": os.path.join(td, "secret.key"),
           "precache_dir": os.path.join(td, "precache"),
           "data_dir": td,
           "nas_music_root": nas,
           "engine_url": "http://127.0.0.1:1",  # nothing listens: offline
           "journal_path": os.path.join(td, "play_journal.jsonl"),
           "precache_target_minutes": 1,
           "feeder_enabled": False}
    app = create_app(cfg)
    conn = db.connect(cfg["db_path"])
    scan(conn, nas)
    pid = pl.create_playlist(conn, "Test List")
    pl.add_item(conn, pid, "file", os.path.join(nas, "Highway Song.wav"),
                "Highway Song")

    client = TestClient(app, follow_redirects=False)
    client.post("/setup", data={"username": "op", "password": "longenough",
                                "password2": "longenough"})

    # ---- pages render
    check("dashboard renders", b"Now Playing" in client.get("/").content)
    r = client.get("/playlists")
    check("playlists page lists playlist", b"Test List" in r.content)
    r = client.get(f"/playlists/{pid}")
    check("builder renders items", b"Highway Song" in r.content)
    check("builder 404 for unknown", client.get("/playlists/999")
          .status_code == 404)
    check("pages need auth: fresh client redirected",
          TestClient(app, follow_redirects=False).get("/playlists")
          .status_code == 303)

    # ---- library search
    rows = client.get("/api/library/search?q=highway").json()
    check("search finds by title", len(rows) == 1
          and rows[0]["title"] == "Highway Song")
    check("search empty q -> []",
          client.get("/api/library/search?q=").json() == [])
    check("search no match -> []",
          client.get("/api/library/search?q=zzzz").json() == [])

    # ---- engine-offline degradation (never 500)
    r = client.get("/api/queue")
    check("queue API degrades offline",
          r.status_code == 200 and r.json()["engine_online"] is False)
    r = client.get("/api/engine/status")
    check("status API degrades offline",
          r.status_code == 200 and r.json()["engine_online"] is False)
    r = client.get("/api/health/tiles")
    tiles = {t["name"]: t for t in r.json()}
    check("NAS tile green", tiles["Music library (NAS)"]["state"] == "green")
    check("disk tile present", "Disk space" in tiles)
    check("index tile counts tracks", "2 tracks"
          in tiles["Library index"]["detail"])

    # ---- history / log API (as-aired; empty on a fresh DB but must be a list)
    hist = client.get("/api/history")
    check("history API returns a list",
          hist.status_code == 200 and isinstance(hist.json(), list))

    # ---- settings: station folders + folder browser
    check("settings page renders",
          b"Station folders" in client.get("/settings").content)
    dirs = client.get("/api/settings/dirs").json()
    check("dir settings listed", len(dirs) >= 4
          and all(d["path"] == "" for d in dirs))
    ads = os.path.join(td, "ads")
    os.makedirs(ads)
    r = client.post("/api/settings/dirs",
                    json={"key": "dir_ads", "path": ads})
    check("set folder ok", r.status_code == 200)
    got = {d["key"]: d for d in client.get("/api/settings/dirs").json()}
    check("folder saved + exists flag",
          got["dir_ads"]["path"] == ads and got["dir_ads"]["exists"])
    check("bad folder -> 400", client.post(
        "/api/settings/dirs",
        json={"key": "dir_ads", "path": ads + "-nope"}).status_code == 400)
    check("unknown key -> 400", client.post(
        "/api/settings/dirs",
        json={"key": "dir_evil", "path": ads}).status_code == 400)
    r = client.post("/api/settings/dirs", json={"key": "dir_ads", "path": ""})
    check("clear folder ok", r.status_code == 200)
    roots = client.get("/api/fs/list").json()
    check("fs list drives", any(d.upper().startswith("C:")
                                for d in roots["dirs"]))
    listing = client.get("/api/fs/list",
                         params={"path": td}).json()
    check("fs lists subfolders", "ads" in listing["dirs"]
          and "nas" in listing["dirs"])
    check("fs bad path -> 400", client.get(
        "/api/fs/list", params={"path": td + "-nope"}).status_code == 400)

    # NAS yanked -> red tile
    os.rename(nas, nas + "-gone")
    tiles = {t["name"]: t for t in client.get("/api/health/tiles").json()}
    check("NAS tile goes red when unreachable",
          tiles["Music library (NAS)"]["state"] == "red")

    print(f"GUI SMOKE OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
