"""Playlist CRUD + dynamic item resolver tests.

Run: python tests/test_playlists.py
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient              # noqa: E402

from services.core import auth, db, playlists as pl    # noqa: E402
from services.core.app import create_app               # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def touch(path, mtime=None):
    with open(path, "wb") as f:
        f.write(b"x" * 64)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def main():
    td = tempfile.mkdtemp(prefix="sf-pl-")
    db_path = os.path.join(td, "t.db")
    db.migrate(db_path)
    conn = db.connect(db_path)

    # ---- CRUD
    pid = pl.create_playlist(conn, "Morning Drive")
    a = pl.add_item(conn, pid, "file", r"C:\music\a.mp3", "A")
    b = pl.add_item(conn, pid, "file", r"C:\music\b.mp3", "B")
    c = pl.add_item(conn, pid, "file", r"C:\music\c.mp3", "C")
    items = pl.get_items(conn, pid)
    check("items in order", [i["title"] for i in items] == ["A", "B", "C"])

    pl.add_item(conn, pid, "file", r"C:\music\x.mp3", "X", position=1)
    items = pl.get_items(conn, pid)
    check("insert at position", [i["title"] for i in items]
          == ["A", "X", "B", "C"])

    pl.remove_item(conn, pid, items[1]["id"])  # remove X
    items = pl.get_items(conn, pid)
    check("remove renumbers",
          [i["title"] for i in items] == ["A", "B", "C"]
          and [i["position"] for i in items] == [0, 1, 2])

    pl.reorder_items(conn, pid, [c, a, b])
    check("reorder", [i["title"] for i in pl.get_items(conn, pid)]
          == ["C", "A", "B"])

    dup = pl.duplicate_playlist(conn, pid, "Morning Drive (copy)")
    check("duplicate copies items",
          [i["title"] for i in pl.get_items(conn, dup)] == ["C", "A", "B"])

    pl.delete_playlist(conn, pid)
    check("delete cascades items", pl.get_items(conn, pid) == [])

    # ---- resolver: file
    real = os.path.join(td, "real.mp3")
    touch(real)
    check("file resolves", pl.resolve_item(
        conn, {"item_type": "file", "path": real}) == real)
    check("missing file -> None", pl.resolve_item(
        conn, {"item_type": "file", "path": real + ".nope"}) is None)

    # ---- resolver: folder-newest
    show = os.path.join(td, "show")
    os.makedirs(show)
    now = time.time()
    touch(os.path.join(show, "ep1.mp3"), now - 200)
    touch(os.path.join(show, "ep2.mp3"), now - 100)
    touch(os.path.join(show, "notes.txt"), now)  # non-audio ignored
    check("folder-newest picks newest audio", pl.resolve_item(
        conn, {"item_type": "folder-newest", "path": show})
        == os.path.join(show, "ep2.mp3"))
    touch(os.path.join(show, "ep3.mp3"), now)
    check("folder-newest tracks new arrival", pl.resolve_item(
        conn, {"item_type": "folder-newest", "path": show})
        == os.path.join(show, "ep3.mp3"))
    empty = os.path.join(td, "empty")
    os.makedirs(empty)
    check("empty folder -> None (skip, not failover)", pl.resolve_item(
        conn, {"item_type": "folder-newest", "path": empty}) is None)

    # ---- resolver: folder-rotation (persisted cursor)
    spots = os.path.join(td, "spots")
    os.makedirs(spots)
    for n in ("ad-a.mp3", "ad-b.mp3", "ad-c.mp3"):
        touch(os.path.join(spots, n))
    item = {"item_type": "folder-rotation", "path": spots}
    got = [os.path.basename(pl.resolve_item(conn, item)) for _ in range(4)]
    check("rotation round-robins evenly",
          got == ["ad-a.mp3", "ad-b.mp3", "ad-c.mp3", "ad-a.mp3"])
    conn.close()
    conn2 = db.connect(db_path)  # simulate P2 restart
    check("rotation cursor survives restart",
          os.path.basename(pl.resolve_item(conn2, item)) == "ad-b.mp3")
    os.remove(os.path.join(spots, "ad-c.mp3"))
    got = [os.path.basename(pl.resolve_item(conn2, item)) for _ in range(2)]
    check("rotation copes with shrunk folder",
          got == ["ad-a.mp3", "ad-b.mp3"])
    conn2.close()

    # ---- API (auth required)
    td2 = tempfile.mkdtemp(prefix="sf-pl-web-")
    cfg = {"station_name": "TestFM",
           "db_path": os.path.join(td2, "web.db"),
           "secret_path": os.path.join(td2, "secret.key")}
    client = TestClient(create_app(cfg), follow_redirects=False)
    check("API rejects anonymous",
          client.get("/api/playlists").status_code == 401)
    client.post("/setup", data={"username": "boss", "password": "longenough",
                                "password2": "longenough"})
    r = client.post("/api/playlists", json={"name": "Overnights"})
    check("API create", r.status_code == 201)
    pid = r.json()["id"]
    check("API duplicate name -> 409",
          client.post("/api/playlists",
                      json={"name": "Overnights"}).status_code == 409)
    r = client.post(f"/api/playlists/{pid}/items",
                    json={"item_type": "folder-rotation", "path": spots,
                          "title": "Spot rotation"})
    check("API add dynamic item", r.status_code == 201)
    check("API bad item_type -> 400",
          client.post(f"/api/playlists/{pid}/items",
                      json={"item_type": "magic", "path": "x"})
          .status_code == 400)
    got = client.get(f"/api/playlists/{pid}").json()
    check("API get returns items", len(got["items"]) == 1
          and got["items"][0]["item_type"] == "folder-rotation")
    check("API stale order -> 409",
          client.post(f"/api/playlists/{pid}/order",
                      json={"item_ids": [999]}).status_code == 409)
    check("API 404 unknown playlist",
          client.get("/api/playlists/9999").status_code == 404)

    print(f"PLAYLISTS OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
