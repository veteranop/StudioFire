"""GUI smoke: pages render, library search works, queue/tiles APIs answer
sanely with the engine offline (P2 must degrade, never 500).

Run: python tests/test_gui_smoke.py
"""
import datetime
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
    check("playlists page offers the file-explorer Open",
          b"Open a playlist (.lst)" in r.content)
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

    # ---- reports page + API + CSV export
    check("reports page renders", b"as-aired" in client.get("/reports").content)
    rep = client.get("/api/reports?start=2026-07-01&end=2026-07-01&kind=all")
    check("reports API shape", rep.status_code == 200
          and rep.json()["count"] == 0 and rep.json()["rows"] == [])
    csv = client.get("/api/reports.csv?start=2026-07-01&end=2026-07-01")
    check("reports CSV export", csv.status_code == 200
          and "text/csv" in csv.headers.get("content-type", "")
          and csv.text.startswith("Time,What aired,Type"))

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

    # paths inside the music root are served from the INDEX (fast over a VPN),
    # not a live listdir. nas has the 2 indexed wavs at its root.
    idxl = client.get("/api/fs/list",
                      params={"path": nas, "files": "audio"}).json()
    check("music-root browse served from the index",
          idxl.get("from_index") is True)
    check("index browse lists the indexed files",
          "Highway Song.wav" in idxl["files"]
          and "Blue Morning.wav" in idxl["files"])
    check("index browse shows no phantom subfolders", idxl["dirs"] == [])
    # .lst isn't indexed -> that picker still lists live (not from the index)
    live = client.get("/api/fs/list",
                      params={"path": nas, "files": "lst"}).json()
    check("lst picker bypasses the index (lists live)",
          not live.get("from_index"))

    # ---- user administration + role gating
    users = client.get("/api/users").json()
    check("users list has the first admin",
          any(u["role"] == "admin" for u in users))
    r = client.post("/api/users", json={"username": "dj",
                    "password": "longenough", "role": "basic"})
    check("create Basic user (stored as operator)", r.status_code == 201)
    uid = r.json()["id"]
    check("Basic user has operator role",
          any(u["id"] == uid and u["role"] == "operator"
              for u in client.get("/api/users").json()))
    check("reject short password", client.post("/api/users", json={
          "username": "x", "password": "short", "role": "basic"}
          ).status_code == 400)
    check("reject duplicate username", client.post("/api/users", json={
          "username": "dj", "password": "longenough", "role": "basic"}
          ).status_code == 409)
    # a Basic user can reach settings + edit folders but NOT manage users
    bc = TestClient(app, follow_redirects=False)
    bc.post("/login", data={"username": "dj", "password": "longenough"})
    check("Basic can open the settings page", bc.get("/settings").status_code == 200)
    check("Basic can edit station folders", bc.post("/api/settings/dirs",
          json={"key": "dir_ads", "path": ""}).status_code == 200)
    check("Basic CANNOT list users (403)", bc.get("/api/users").status_code == 403)
    check("Basic CANNOT create users (403)", bc.post("/api/users", json={
          "username": "z", "password": "longenough", "role": "basic"}
          ).status_code == 403)
    # admin: promote, reset password, delete
    check("promote Basic -> Admin",
          client.post(f"/api/users/{uid}/role", json={"role": "admin"}
                      ).status_code == 200)
    check("reset a user's password",
          client.post(f"/api/users/{uid}/password",
                      json={"password": "anotherlong"}).status_code == 200)
    check("delete a user", client.delete(f"/api/users/{uid}").status_code == 200)
    me = next(u for u in client.get("/api/users").json() if u["role"] == "admin")
    check("can't delete the last admin / yourself",
          client.delete(f"/api/users/{me['id']}").status_code == 400)

    # ---- station equipment (ICMP monitor)
    check("devices list starts empty", client.get("/api/devices").json() == [])
    r = client.post("/api/devices", json={"name": "Loopback", "host": "127.0.0.1"})
    check("add a device", r.status_code == 201)
    devid = r.json()["id"]
    check("device add needs name + host",
          client.post("/api/devices", json={"name": "", "host": ""}
                      ).status_code == 400)
    devs = client.get("/api/devices").json()
    check("device listed with a status shape",
          len(devs) == 1 and devs[0]["host"] == "127.0.0.1"
          and "up" in devs[0])
    # ping-now: localhost is always reachable
    pong = client.post(f"/api/devices/{devid}/ping").json()
    check("ping-now reports localhost reachable", pong["up"] is True)
    check("ping-now 404 for unknown device",
          client.post("/api/devices/999/ping").status_code == 404)
    check("delete a device", client.delete(f"/api/devices/{devid}").status_code == 200)
    check("devices empty again after delete",
          client.get("/api/devices").json() == [])

    # ---- schedule calendar page + API
    check("schedule page renders", b"cal-weeks" in client.get("/schedule").content)
    cal = client.get("/api/calendar?month=2026-08").json()
    check("calendar returns the requested month",
          cal["year"] == 2026 and cal["month"] == 8
          and cal["month_name"] == "August")
    check("calendar has a cell per day (Aug = 31)", len(cal["days"]) == 31)
    check("calendar day carries a shows list (spots excluded)",
          "shows" in cal["days"][0] and "spots" not in cal["days"][0])
    check("calendar bad month falls back to current",
          client.get("/api/calendar?month=nope").json()["month"]
          == datetime.date.today().month)
    from services.core import schedule as _sched
    _sched.add(conn, "playlist", playlist_id=pid, start_at="2026-08-15T09:00")
    cal2 = client.get("/api/calendar?month=2026-08").json()
    day15 = next(d for d in cal2["days"] if d["day"] == 15)
    check("a scheduled show lands on its calendar day",
          any(s["time"] == "09:00" for s in day15["shows"]))

    # NAS yanked -> red tile
    os.rename(nas, nas + "-gone")
    tiles = {t["name"]: t for t in client.get("/api/health/tiles").json()}
    check("NAS tile goes red when unreachable",
          tiles["Music library (NAS)"]["state"] == "red")

    print(f"GUI SMOKE OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
