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
           "secret_path": os.path.join(td2, "secret.key"),
           "data_dir": td2,
           "precache_dir": os.path.join(td2, "precache"),
           "engine_url": "http://127.0.0.1:1",
           "journal_path": os.path.join(td2, "journal.jsonl"),
           "feeder_enabled": False,
           "path_aliases": {"\\\\STUDIO\\music": "Z:"}}
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

    # ---- ZaraRadio .lst import
    lst = ("3\r\n"
           "215457\t\\\\NAS\\music\\A\\song one.mp3\r\n"
           "0\t.time\r\n"                       # Zara special token: skipped
           "353906\t\\\\NAS\\music\\B\\caf\xe9 groove.mp3\r\n"
           "not a track line\r\n"
           "126249\t\\\\NAS\\music\\C\\notes.txt\r\n")  # non-audio: skipped
    entries = pl.parse_lst(lst.encode("cp1252"))
    check("lst: header/junk skipped, audio kept",
          [e["title"] for e in entries] == ["song one", "caf\xe9 groove"])
    entries2 = pl.parse_lst(lst.encode("utf-8"))
    check("lst: utf-8 also accepted",
          [e["title"] for e in entries2] == ["song one", "caf\xe9 groove"])
    r = client.post("/api/playlists/import_lst",
                    files={"file": ("show.lst", lst.encode("cp1252"))},
                    data={"name": "Zara Import"})
    check("lst API: imports as new playlist",
          r.status_code == 201 and r.json()["imported"] == 2)
    lid = r.json()["id"]
    got = client.get(f"/api/playlists/{lid}").json()
    check("lst API: items in order, type=file",
          [i["title"] for i in got["items"]] == ["song one", "caf\xe9 groove"]
          and all(i["item_type"] == "file" for i in got["items"]))
    check("lst API: duplicate name -> 409",
          client.post("/api/playlists/import_lst",
                      files={"file": ("show.lst", lst.encode("cp1252"))},
                      data={"name": "Zara Import"}).status_code == 409)
    check("lst API: empty file -> 400",
          client.post("/api/playlists/import_lst",
                      files={"file": ("junk.lst", b"1014\r\nrubbish\r\n")},
                      data={"name": "Junk"}).status_code == 400)
    alias_lst = b"1\r\n1000\t\\\\STUDIO\\music\\G\\artist\\track.mp3\r\n"
    r = client.post("/api/playlists/import_lst",
                    files={"file": ("a.lst", alias_lst)},
                    data={"name": "Alias Import"})
    got = client.get(f"/api/playlists/{r.json()['id']}").json()
    check("lst API: path alias rewrites to local drive",
          got["items"][0]["path"] == "Z:\\G\\artist\\track.mp3")

    # ---- backup / restore
    import json
    r = client.get("/api/backup")
    check("backup downloads as attachment",
          r.status_code == 200
          and "attachment" in r.headers.get("content-disposition", ""))
    data = r.json()
    check("backup contains playlist + items",
          data["studiofire_backup"] == 1
          and any(p["name"] == "Overnights" and len(p["items"]) == 1
                  for p in data["playlists"]))
    r = client.post("/api/restore", files={
        "file": ("b.json", json.dumps(data).encode(), "application/json")})
    check("restore imports (renamed on conflict)",
          r.status_code == 200 and r.json()["imported"] == 3)
    names = [p["name"] for p in client.get("/api/playlists").json()]
    check("restored copy present", "Overnights (restored)" in names)
    check("restored copy kept its items",
          any(p["name"] == "Overnights (restored)"
              and p["item_count"] == 1
              for p in client.get("/api/playlists").json()))
    r = client.post("/api/restore", files={
        "file": ("x.json", b"not json at all", "application/json")})
    check("restore rejects garbage", r.status_code == 400)
    r = client.post("/api/restore", files={
        "file": ("y.json", b"{\"other\": true}", "application/json")})
    check("restore rejects non-backup JSON", r.status_code == 400)

    # ---- relink_broken: repoint stale (out-of-root) paths to indexed files
    rpath = os.path.join(td, "relink.db")
    db.migrate(rpath)
    rc = db.connect(rpath)
    for p in [r"Z:/G\Artist\song1.mp3", r"Z:/G\Other\song2.mp3",
              r"Z:/G\A\dup.mp3", r"Z:/G\B\dup.mp3"]:
        rc.execute("INSERT INTO tracks (path, indexed_at, missing) "
                   "VALUES (?, ?, 0)", (p, time.time()))
    rc.commit()
    rpid = pl.create_playlist(rc, "Legacy")
    pl.add_item(rc, rpid, "file", r"Z:\Local Disk\KDPI\song1.mp3", "one")  # stale
    pl.add_item(rc, rpid, "file", r"Z:\G\Other\song2.mp3", "two")   # in place
    pl.add_item(rc, rpid, "file", r"Z:\Old\dup.mp3", "dup")         # ambiguous
    pl.add_item(rc, rpid, "file", r"Z:\Old\missing.mp3", "gone")    # unmatched
    stats = pl.relink_broken(rc, "Z:/G", rpid)
    check("relink: correct counts",
          stats["relinked"] == 1 and stats["in_place"] == 1
          and stats["ambiguous"] == 1 and stats["unmatched"] == 1)
    paths = {i["title"]: i["path"] for i in pl.get_items(rc, rpid)}
    check("relink: stale item repointed into the music root",
          os.path.normcase(os.path.normpath(paths["one"]))
          == os.path.normcase(os.path.normpath(r"Z:/G\Artist\song1.mp3")))
    check("relink: in-place item untouched", paths["two"] == r"Z:\G\Other\song2.mp3")
    check("relink: ambiguous name left as-is", paths["dup"] == r"Z:\Old\dup.mp3")
    check("relink: unmatched reported",
          "missing.mp3" in stats["unmatched_examples"])
    rc.close()

    # ---- ZaraRadio .lst mirror (export format + round-trip + rename/delete)
    lc = db.connect(db_path)
    lpid = pl.create_playlist(lc, "Drive Time")
    p1 = r"Z:/G\Artist\Album\01 Song One.mp3"
    p2 = r"Z:/G\Artist\Album\02 Song Two.mp3"
    pl.add_item(lc, lpid, "file", p1, "Song One")
    pl.add_item(lc, lpid, "file", p2, "Song Two")
    # give one track a known duration in the index (the other stays unknown -> 0)
    lc.execute("INSERT INTO tracks (path, title, duration_sec, indexed_at, "
               "missing) VALUES (?, 'Song One', 213.4, 0, 0)", (p1,))
    lc.commit()

    text = pl.export_lst_text(lc, lpid)
    lines = text.replace("\r\n", "\n").rstrip("\n").split("\n")
    check("lst header is the track count", lines[0] == "2")
    check("lst duration is ms from the index (213.4s -> 213400)",
          lines[1].split("\t")[0] == "213400")
    check("lst unknown duration is 0", lines[2].split("\t")[0] == "0")
    check("lst paths use backslashes (Zara style)",
          lines[1].split("\t")[1] == r"Z:\G\Artist\Album\01 Song One.mp3")
    check("lst is CRLF terminated", text.endswith("\r\n") and "\r\n" in text)
    # our own parser reads it back to the same paths
    reparsed = pl.parse_lst(text.encode("cp1252"))
    check("lst round-trips through parse_lst",
          [e["path"] for e in reparsed]
          == [r"Z:\G\Artist\Album\01 Song One.mp3",
              r"Z:\G\Artist\Album\02 Song Two.mp3"])

    lst_dir = os.path.join(td, "lst_out")
    os.makedirs(lst_dir)
    written = pl.write_lst(lc, lst_dir, lpid)
    check("write_lst creates <name>.lst",
          written == os.path.join(lst_dir, "Drive Time.lst")
          and os.path.isfile(written))
    check("write_lst is a no-op when the folder is unset/missing",
          pl.write_lst(lc, "", lpid) is None
          and pl.write_lst(lc, os.path.join(td, "nope"), lpid) is None)

    # rename mirrors to the new name and removes the old file
    pl.rename_playlist(lc, lpid, "Evening Show")
    pl.write_lst(lc, lst_dir, lpid, old_name="Drive Time")
    check("rename writes the new .lst",
          os.path.isfile(os.path.join(lst_dir, "Evening Show.lst")))
    check("rename removes the old .lst",
          not os.path.isfile(os.path.join(lst_dir, "Drive Time.lst")))

    # unsafe filename characters are sanitised
    check("lst filename sanitises path chars",
          pl.lst_filename('Rock/Pop: "Best"?') == "Rock_Pop_ _Best__.lst")

    # sync_all writes every playlist in the DB
    pl.create_playlist(lc, "Another")
    total = lc.execute("SELECT COUNT(*) FROM playlists").fetchone()[0]
    n = pl.sync_all_lst(lc, lst_dir)
    check("sync_all_lst writes every playlist", n == total and total >= 2)
    check("sync_all wrote the new playlist's file",
          os.path.isfile(os.path.join(lst_dir, "Another.lst")))

    pl.remove_lst(lst_dir, "Evening Show")
    check("remove_lst deletes the file",
          not os.path.isfile(os.path.join(lst_dir, "Evening Show.lst")))
    lc.close()

    print(f"PLAYLISTS OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
