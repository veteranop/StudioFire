"""Library duplicate finder + safe cleanup (ticket 703).

Run: python tests/test_duplicates.py
"""
import datetime
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient              # noqa: E402

from services.core import db, duplicates as dup      # noqa: E402
from services.core import playlists as pl            # noqa: E402
from services.core.app import create_app             # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def _track(conn, path, artist=None, title=None, size=1000, dur=200.0,
           missing=0):
    conn.execute(
        "INSERT INTO tracks (path, title, artist, album, duration_sec, "
        "  format, size, mtime, indexed_at, missing, tags_read) "
        "VALUES (?, ?, ?, NULL, ?, 'MP3', ?, ?, ?, ?, 1)",
        (path, title, artist, dur, size, time.time(), time.time(), missing))


def _w(path, n=64):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * n)


def main():
    td = tempfile.mkdtemp(prefix="sf-dup-")
    root = os.path.join(td, "music")
    db_path = os.path.join(td, "t.db")
    db.migrate(db_path)
    conn = db.connect(db_path)

    # real files on disk
    a = os.path.join(root, "a", "song.mp3")
    b = os.path.join(root, "b", "song (1).mp3")
    c = os.path.join(root, "c", "song.mp3")
    solo = os.path.join(root, "d", "solo.mp3")
    adsd = os.path.join(root, "ads")
    sad = os.path.join(root, "a", "dup.mp3")
    sad2 = os.path.join(adsd, "dup.mp3")
    for p in (a, b, c, solo, sad, sad2):
        _w(p)

    _track(conn, a, "Artist", "Song", size=1000)              # tagged
    _track(conn, b, None, None, size=1200)                   # untagged, bigger
    _track(conn, c, None, None, size=800)
    _track(conn, solo, None, None, size=500)
    _track(conn, sad, None, None, size=700)
    _track(conn, sad2, None, None, size=700)                 # inside dir_ads
    conn.commit()

    # ads folder is a station folder -> its files are protected
    db.set_setting(conn, "dir_ads", adsd)
    # a playlist uses one of the "song" copies -> that one is protected too
    pid = pl.create_playlist(conn, "Refs")
    pl.add_item(conn, pid, "file", b, "Song")               # references copy b

    # ---- find_duplicates
    res = dup.find_duplicates(conn, root)
    bykey = {g["key"]: g for g in res["groups"]}
    check("groups the three 'song' copies (suffix + case ignored)",
          "song" in bykey and bykey["song"]["count"] == 3)
    check("the lone file is not a group", "solo" not in bykey)
    song = bykey["song"]
    by_path = {os.path.normcase(f["path"]): f for f in song["files"]}
    check("a playlist-referenced copy is flagged protected + used_by",
          by_path[os.path.normcase(b)]["protected"] is True
          and by_path[os.path.normcase(b)]["used_by"] == ["Refs"])
    check("exactly one suggested keep", 
          sum(1 for f in song["files"] if f["suggested_keep"]) == 1)
    check("the referenced copy is the suggested keep",
          by_path[os.path.normcase(b)]["suggested_keep"] is True)
    check("size is reported for sizing decisions",
          by_path[os.path.normcase(b)]["size"] == 1200)
    check("counts summarise the preview",
          res["group_count"] == len(res["groups"])
          and res["file_count"] == sum(g["count"] for g in res["groups"]))
    dupg = bykey.get("dup")
    dup_prot = {f["path"]: f["protected"] for f in (dupg["files"] if dupg else [])}
    check("a file inside a station folder is protected; a stray is not",
          dupg and dup_prot.get(sad2) is True and dup_prot.get(sad) is False)

    # missing files never appear
    conn.execute("UPDATE tracks SET missing = 1 WHERE path = ?", (c,))
    conn.commit()
    res2 = dup.find_duplicates(conn, root)
    still = {g["key"]: g for g in res2["groups"]}
    check("missing rows are excluded from the preview",
          still["song"]["count"] == 2)
    conn.execute("UPDATE tracks SET missing = 0 WHERE path = ?", (c,))
    conn.commit()

    # ---- quarantine: moves (never deletes), mirrors layout, writes manifest
    res = dup.find_duplicates(conn, root)
    song = next(g for g in res["groups"] if g["key"] == "song")
    to_remove = [f["path"] for f in song["files"]
                 if not f["suggested_keep"] and not f["protected"]]
    out = dup.quarantine(conn, to_remove, root)
    check("unticked copies were moved", len(out["moved"]) == len(to_remove))
    check("source files are gone from the library (moved, not copied)",
          all(not os.path.isfile(p) for p in to_remove))
    check("moved copies land under _Duplicates/<date>, layout mirrored",
          all(out["dest"] and os.path.isfile(m["to"]) for m in out["moved"])
          and os.path.dirname(out["moved"][0]["to"]).startswith(out["dest"]))
    check("the kept copy is untouched", os.path.isfile(b))
    mpath = os.path.join(out["dest"], "manifest.json")
    check("a manifest enables putting them back", os.path.isfile(mpath))
    manifest = json.load(open(mpath, encoding="utf-8"))
    check("manifest records from -> to for every moved file",
          len(manifest) == len(out["moved"])
          and all("from" in m and "to" in m for m in manifest))
    check("moved files are marked missing in the index",
          all(conn.execute("SELECT missing FROM tracks WHERE path = ?",
                           (p,)).fetchone()["missing"] == 1
              for p in to_remove))

    # ---- quarantine refuses to move a protected file; reports a missing one
    pro = dup.quarantine(conn, [b, os.path.join(root, "nope.mp3")], root)
    check("a protected file is skipped, never moved",
          pro["moved"] == []
          and len(pro["skipped"]) == 1
          and os.path.isfile(b))
    check("a missing file is reported as an error",
          any("nope.mp3" in e["path"] for e in pro["errors"]))

    conn.close()

    # ---- API + page
    cfg = {"station_name": "TestFM", "db_path": db_path,
           "secret_path": os.path.join(td, "secret.key"), "data_dir": td,
           "precache_dir": os.path.join(td, "precache"),
           "nas_music_root": root, "engine_url": "http://127.0.0.1:1",
           "journal_path": os.path.join(td, "j.jsonl"),
           "precache_target_minutes": 1, "feeder_enabled": False}
    client = TestClient(create_app(cfg), follow_redirects=False)
    check("duplicates page needs auth",
          client.get("/duplicates").status_code == 303)
    client.post("/setup", data={"username": "op", "password": "longenough",
                                "password2": "longenough"})
    check("duplicates page renders", b"Duplicate cleanup" in
          client.get("/duplicates").content)
    got = client.get("/api/duplicates").json()
    check("GET /api/duplicates returns groups",
          "groups" in got and isinstance(got["groups"], list))
    check("settings links to the cleanup", b"Open duplicate cleanup" in
          client.get("/settings").content)
    bad = client.post("/api/duplicates/quarantine", json={})
    check("quarantine needs a path list (400)", bad.status_code == 400)
    check("quarantine endpoint needs auth",
          TestClient(create_app(cfg), follow_redirects=False)
          .post("/api/duplicates/quarantine", json={"remove": ["x"]})
          .status_code == 401)

    print("\nDUPLICATES OK (%d checks)" % passed)


if __name__ == "__main__":
    main()
