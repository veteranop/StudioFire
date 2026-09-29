"""Drive-letter-independent path resolution (the KDPI Z:-drop fix).

Covers:
  * services/pathmap.py — the pure resolver + share-root derivation + the
    per-session drive-exists cache.
  * services.core.playlists.alias_path / set_path_aliases — the wiring the rest
    of core uses, including that a stored Z:\\ folder resolves at feed time so a
    spot rule finds its files when the drive mapping is gone (the exact 132-error
    production failure from 2026-09-29).
  * /api/fs/list — the file picker offers the NAS root(s) with no Z: mapping, so
    it can no longer show "only C:\\" (the reported bug).

Run: python tests/test_pathmap.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient                     # noqa: E402

from services import pathmap                                  # noqa: E402
from services.core import db, playlists as pl                 # noqa: E402
from services.core.app import create_app                      # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * 64)


def set_drive(letter, exists):
    """Force the process-lifetime drive-exists answer so the test never depends
    on which drives the CI/dev box actually has."""
    pathmap.clear_drive_cache()
    pathmap._DRIVE_EXISTS[letter.upper()] = exists


# --------------------------------------------------------------- pure resolver

def test_share_root():
    # config writes forward-slash UNC; drive-map.bat maps Z: -> the PARENT of
    # nas_music_root, so the share root is nas_music_root minus its last part.
    check("share_root: //host/share/G -> \\\\host\\share",
          pathmap.share_root("//KDPI-Media/music/G")
          == r"\\KDPI-Media\music")
    check("share_root: backslash UNC in, canonical out",
          pathmap.share_root(r"\\KDPI-Media\music\G")
          == r"\\KDPI-Media\music")
    check("share_root: trailing slash tolerated",
          pathmap.share_root("//KDPI-Media/music/G/")
          == r"\\KDPI-Media\music")
    check("share_root: empty -> empty", pathmap.share_root("") == "")
    check("share_root: None -> empty", pathmap.share_root(None) == "")
    # a station whose nas_music_root is itself a drive path (e.g. Z:/G) gets a
    # drive-shaped parent back, so the fallback stays a harmless no-op instead
    # of inventing a bogus \\Z:\G
    check("share_root: drive-based music root -> drive-shaped parent",
          pathmap.share_root("Z:/G") == "Z:")
    check("share_root: local-disk music root -> its parent",
          pathmap.share_root(r"C:\temp\music\G") == r"C:\temp\music")
    check("share_root: already a share root -> itself",
          pathmap.share_root("//HOST/share") == r"\\HOST\share")


def test_resolve():
    nas = "//KDPI-Media/music/G"

    set_drive("Z", False)  # the reboot dropped the mapping
    check("resolve: missing-drive path re-points at the share root",
          pathmap.resolve(r"Z:\John\New PSAs", {}, nas)
          == r"\\KDPI-Media\music\John\New PSAs")
    check("resolve: forward-slash drive path also handled",
          pathmap.resolve("Z:/John/New PSAs", {}, nas)
          == r"\\KDPI-Media\music\John\New PSAs")
    check("resolve: bare drive root -> the share root itself",
          pathmap.resolve("Z:\\", {}, nas) == r"\\KDPI-Media\music")

    set_drive("Z", True)  # mapping present -> leave the path exactly alone
    check("resolve: present-drive path untouched",
          pathmap.resolve(r"Z:\John\New PSAs", {}, nas)
          == r"Z:\John\New PSAs")

    set_drive("Z", False)
    check("resolve: no NAS root -> unchanged even with the drive gone",
          pathmap.resolve(r"Z:\John\New PSAs", {}, "")
          == r"Z:\John\New PSAs")
    check("resolve: explicit alias wins over the fallback",
          pathmap.resolve(r"Z:\John\x.mp3",
                          {"Z:\\": "\\\\OTHER\\vol\\"}, nas)
          == r"\\OTHER\vol\John\x.mp3")
    check("resolve: UNC path (no drive letter) untouched",
          pathmap.resolve(r"\\KDPI-Media\music\G\a.mp3", {}, nas)
          == r"\\KDPI-Media\music\G\a.mp3")
    check("resolve: empty stays empty", pathmap.resolve("", {}, nas) == "")

    set_drive("C", True)  # a real local drive is never rewritten
    check("resolve: existing local drive left alone",
          pathmap.resolve(r"C:\other\x.mp3", {}, nas) == r"C:\other\x.mp3")


# ------------------------------------ playlists wiring: the spot-folder failure

def test_spot_folder_resolves_without_drive():
    """A spot rule whose folder_path is Z:\\... must resolve to real files when
    the drive is absent — before the fix, insert_spot's bare os.path.isdir on the
    stored Z: value returned False and the spot 'folder is not there' (132 misses
    at KDPI on 2026-09-29)."""
    td = tempfile.mkdtemp(prefix="sf-pathmap-")
    dbp = os.path.join(td, "t.db")
    db.migrate(dbp)
    conn = db.connect(dbp)

    # nas_music_root points a level deeper than the share root, exactly like the
    # box (Z: == \\host\music, nas_music_root == \\host\music\G).
    nas_root = os.path.join(td, "music", "G")
    share = pathmap.share_root(nas_root)           # -> <td>\music
    psas = os.path.join(share, "John", "New PSAs")
    touch(os.path.join(psas, "legal-id-01.mp3"))

    pl.set_path_aliases({}, nas_root)
    try:
        set_drive("Z", False)
        folder = r"Z:\John\New PSAs"
        resolved = pl.alias_path(folder)
        check("alias_path: stored Z: spot folder resolves to the share",
              os.path.normcase(resolved) == os.path.normcase(psas))
        check("resolved spot folder now passes os.path.isdir "
              "(the exact production check)", os.path.isdir(resolved))
        picked = pl.resolve_item(
            conn, {"item_type": "folder-rotation", "path": folder})
        check("resolve_item finds a file in the resolved Z: folder",
              picked is not None
              and os.path.basename(picked) == "legal-id-01.mp3")

        set_drive("Z", True)  # mapping back -> the raw Z: path is left intact
        check("alias_path: with Z: present the stored path is untouched",
              pl.alias_path(folder) == folder)
    finally:
        pl.set_path_aliases({})   # don't leak globals into other tests
        pathmap.clear_drive_cache()
        conn.close()


# ---------------------------------------------- /api/fs/list offers the NAS root

def test_fs_list_offers_nas_root():
    td = tempfile.mkdtemp(prefix="sf-pathmap-web-")
    cfg = {"station_name": "TestFM",
           "db_path": os.path.join(td, "t.db"),
           "secret_path": os.path.join(td, "secret.key"),
           "precache_dir": os.path.join(td, "precache"),
           "engine_url": "http://127.0.0.1:59999",   # unused; never dialed here
           "journal_path": os.path.join(td, "j.jsonl"),
           "nas_music_root": "//TESTNAS/share/G",
           "precache_target_minutes": 0.15,
           "feeder_enabled": False}
    app = create_app(cfg)
    client = TestClient(app, follow_redirects=False)
    client.post("/setup", data={"username": "boss", "password": "longenough",
                                "password2": "longenough"})

    # make the drive probe deterministic: only C:\ "exists"
    real_exists = os.path.exists

    def fake_exists(p):
        if len(p) == 3 and p[1:] == ":\\":
            return p.upper() == "C:\\"
        return real_exists(p)

    os.path.exists = fake_exists
    try:
        r = client.get("/api/fs/list")
    finally:
        os.path.exists = real_exists
    check("fs/list empty path OK", r.status_code == 200)
    dirs = r.json()["dirs"]
    check("fs/list still lists the local drive (no regression)", "C:\\" in dirs)
    check("fs/list now offers the NAS share root (the reported-bug fix)",
          r"\\TESTNAS\share" in dirs)


def main():
    test_share_root()
    test_resolve()
    test_spot_folder_resolves_without_drive()
    test_fs_list_offers_nas_root()
    print(f"PATHMAP OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
