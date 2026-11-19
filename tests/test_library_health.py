r"""Library health — the numbers the Playlists page and the missing report show.

The incident this answers (TimeTrax 996 / 1036 / 1052): the NAS library was
reorganised, 241 of 366 songs on the on-air playlist went dark, and the station
looped five leftover cache clips for 1h41m. The index knew; the screen did not.

Two rules make the numbers trustworthy, and both are tested here:
  * An index row flagged `missing=1` was PROVEN gone by the indexer (its folder
    was readable). A path the index has never seen is NOT evidence of death —
    it is 'not indexed', shown in its own column, never added to "can't be found".
  * Every comparison is path-NORMALISED. The index stores `Z:/G\Artist\x.mp3`
    and the playlists store `Z:\G\Artist\x.mp3` for the same file; an exact join
    calls 93% of the on-air box's playlist items dead (measured 2026-11-19).

Run: python tests/test_library_health.py   (~2s)
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient                             # noqa: E402

from services.core import db, library_health as lh                    # noqa: E402
from services.core import playlists as pl, reports                    # noqa: E402
from services.core.app import create_app                              # noqa: E402

passed = 0


def check(name, cond, extra=""):
    global passed
    if not cond:
        print("FAIL:", name, extra)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def track(conn, path, missing=0, size=1000, dur=180.0, fmt="mp3"):
    with conn:
        conn.execute(
            "INSERT INTO tracks(path, title, duration_sec, format, size, mtime, "
            "  indexed_at, missing) VALUES (?,?,?,?,?,?,?,?)",
            (path, os.path.basename(path.replace("/", "\\")), dur, fmt, size,
             1_700_000_000.0, 1_700_000_100.0, missing))


def item(conn, pid, path, title=None):
    return pl.add_item(conn, pid, "file", path,
                       title or os.path.basename(path.replace("/", "\\")))


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        dbp = os.path.join(td, "core.db")
        db.migrate(dbp)
        conn = db.connect(dbp)

        # ---- a library, spelled the way the station's tools spell it ------
        lib = os.path.join(td, "lib")
        os.makedirs(os.path.join(lib, "Alpha"), exist_ok=True)
        os.makedirs(os.path.join(lib, "Beta"), exist_ok=True)
        present = os.path.join(lib, "Alpha", "one.mp3")
        for p in (present, os.path.join(lib, "Beta", "two.mp3")):
            open(p, "wb").write(b"ID3")
        # the index stores the BACKSLASH form...
        track(conn, present)                                  # playable
        track(conn, os.path.join(lib, "Beta", "two.mp3"))      # playable
        track(conn, os.path.join(lib, "Alpha", "gone.mp3"), missing=1,
              size=2222)                                      # proved gone
        # ...and a candidate for "moved": same name + same parent folder name
        track(conn, os.path.join(lib, "NewAlpha", "gone.mp3"), size=2222)
        # a decoy with the same file name in a totally different folder
        track(conn, os.path.join(lib, "Decoys", "other.mp3"), size=999)
        track(conn, os.path.join(lib, "Other", "one.mp3"), size=5555)

        keep = pl.create_playlist(conn, "Keepers")
        item(conn, keep, present)                              # exact match
        item(conn, keep, present.replace("\\", "/"))           # SLASH form: must play
        item(conn, keep, os.path.join(lib, "Alpha", "gone.mp3"))   # proved gone
        item(conn, keep, os.path.join(lib, "Alpha", "never.mp3"))  # not indexed
        item(conn, keep, os.path.join(lib, "NoFolder", "ghost.mp3"))

        cfg = {"nas_music_root": lib}

        # ---- 1. the join is normalised -----------------------------------
        h = lh.health(conn, cfg)
        row = next(r for r in h["playlists"] if r["id"] == keep)
        check("5 file items counted", row["items"] == 5, str(row))
        check("the forward-slash item counts as playable (normalised join)",
              row["playable"] == 2, str(row))
        check("index-proved-gone items are 'missing'", row["missing"] == 1, str(row))
        check("a path the index never saw is 'not indexed', NOT missing",
              row["not_indexed"] == 2, str(row))
        check("percent follows missing/items", row["pct_unplayable"] == 20.0, str(row))

        # ---- 2. same file, different spelling: duration join -------------
        ids = [r["id"] for r in pl.get_items(conn, keep)]
        durs = pl.item_durations(conn, keep)
        check("item_durations resolves the forward-slash item via the index",
              ids[1] in durs, str(sorted(durs)))
        check("...and still leaves a file with no index row unknown",
              ids[3] not in durs)

        # ---- 3. snapshot --------------------------------------------------
        s = h["snapshot"]
        check("snapshot sees the whole index", s["tracks_total"] == 6, str(s))
        check("snapshot counts present vs flagged", s["tracks_present"] == 5
              and s["tracks_missing"] == 1, str(s))
        check("snapshot reports the playlist-referenced missing count",
              s["playlist_missing_paths"] == 1, str(s["playlist_missing_paths"]))

        # ---- 4. on-air playlist is pinned first --------------------------
        with conn:
            conn.execute("INSERT INTO settings(key, value) VALUES"
                         "('active_playlist_id', ?) ON CONFLICT(key) DO UPDATE "
                         "SET value=excluded.value", (str(keep),))
        other = pl.create_playlist(conn, "AAA worse")   # sorts first by name
        for p in ("x1.mp3", "x2.mp3", "x3.mp3"):
            item(conn, other, os.path.join(lib, "Alpha", p))
        h = lh.health(conn, cfg)
        check("the on-air playlist is pinned above a worse-sorting one",
              h["playlists"][0]["id"] == keep, str([r["name"] for r in h["playlists"]]))
        check("...even though the other list has more missing",
              h["playlists"][1]["id"] == other)

        # ---- 5. missing(): grouped by folder, with the ladder hint -------
        m = lh.missing(conn, cfg)
        check("missing() counts only non-playable items", m["files"] == 6,
              str(m["files"]))
        folders = [g["folder"] for g in m["groups"]]
        check("groups are sorted by size then name", folders[0].endswith("Alpha"),
              str(folders[:3]))
        gone = [f for g in m["groups"] for f in g["files"]
                if f["title"] == "gone.mp3"]
        check("the full path is carried, not a basename", all(f["path"].startswith(lib)
                                                              for f in gone))
        verdicts = {f["hint"] for f in gone}
        check("a same-name + same-folder file is only a HINT, never a fact",
              "moved" in verdicts, str(verdicts))
        check("the hint names the candidate path, not a guess at the song",
              any(f["candidate"] and "NewAlpha" in f["candidate"] for f in gone),
              str([f["candidate"] for f in gone]))

        # ---- 6. reports ---------------------------------------------------
        rep = reports.run("missing-from-playlists", conn, "2026-11-01",
                          "2026-11-02", cfg=cfg)
        check("the report lists every non-playable item", len(rep["rows"]) == 6,
              str(len(rep["rows"])))
        check("the report carries the FULL path column",
              all(r["path"] for r in rep["rows"]))
        check("the report says nothing was changed",
              "Nothing has been changed" in rep["note"])
        check("a reachable library is stated plainly",
              "Library checked as of the last full scan" in rep["note"], rep["note"][:80])

        idx = reports.run("library-index", conn, "2026-11-01", "2026-11-02", cfg=cfg)
        vals = {r["item"]: r["value"] for r in idx["rows"]}
        check("index report shows the playlist number that matters",
              vals["Playlist items that cannot be found"] == "1", str(vals))
        check("index report keeps the index figure secondary",
              vals["Files the index marks missing"] == "1", str(vals))
        check("index report names the share state", "reachable" in
              list(vals)[0].lower(), str(list(vals)[:2]))

        # ---- 7. an unreachable library must not look like mass deletion --
        gone_cfg = {"nas_music_root": os.path.join(td, "no-such-share")}
        h2 = lh.health(conn, gone_cfg)
        check("health marks the library unreachable", lh.reachable(gone_cfg) is False)
        row2 = next(r for r in h2["playlists"] if r["id"] == keep)
        check("...and still reports the last known counts, not zeros",
              row2["missing"] == 1, str(row2))
        rep2 = reports.run("missing-from-playlists", conn, "2026-11-01",
                           "2026-11-02", cfg=gone_cfg)
        check("the report leads with 'could not be reached'",
              "could not be reached" in rep2["note"], rep2["note"][:80])

        # ---- 8. nothing to report is a GOOD day --------------------------
        clean = pl.create_playlist(conn, "All good")
        item(conn, clean, present)
        empty = lh.missing(conn, cfg)
        check("a playable item never appears in missing()",
              all(f["title"] != "one.mp3" or f["path"] != present
                  for g in empty["groups"] for f in g["files"]))

        # ---- 9. the API the page calls -----------------------------------
        webtd = os.path.join(td, "web")
        os.makedirs(os.path.join(webtd, "precache"), exist_ok=True)
        app_cfg = {"station_name": "HealthTest", "db_path": dbp,
                   "secret_path": os.path.join(webtd, "secret.key"),
                   "precache_dir": os.path.join(webtd, "precache"),
                   "engine_url": "http://127.0.0.1:9",
                   "journal_path": os.path.join(webtd, "play_journal.jsonl"),
                   "feeder_enabled": False, "nas_music_root": lib}
        client = TestClient(create_app(app_cfg), follow_redirects=False)
        client.post("/setup", data={"username": "boss", "password": "longenough",
                                    "password2": "longenough"})
        r = client.get("/api/library/health")
        check("GET /api/library/health answers", r.status_code == 200,
              str(r.status_code))
        d = r.json()
        check("...with a snapshot and one row per playlist",
              d["snapshot"]["tracks_total"] == 6 and len(d["playlists"]) >= 3,
              str(list(d)[:4]))
        r = client.get("/api/library/missing")
        check("GET /api/library/missing answers", r.status_code == 200,
              str(r.status_code))
        check("...grouped, with statuses",
              r.json()["groups"] and "status" in r.json()["groups"][0]["files"][0])
        r = client.get(f"/api/library/playlist/{keep}/flags")
        check("the editor can ask for per-item flags", r.status_code == 200
              and set(r.json()["flags"].values()) <= {"playable", "missing",
                                                      "not_indexed"})

        conn.close()

    print(f"LIBRARY HEALTH OK ({passed} checks)")


if __name__ == "__main__":
    main()
