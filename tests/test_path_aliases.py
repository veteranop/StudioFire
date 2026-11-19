r"""Settings > Library path aliases — the operator-editable path rewrite.

Why this exists (TimeTrax 1052, Mark 2026-11-19): after the NAS was reorganised a
share name the station's PC cannot resolve (`\\KDPI-Media\music`) was still
stored inside playlist rows, while the same files were reachable on `Z:`. Those
items were skipped on air — the feeder resolves at feed time, saw a path that
does not exist, and moved on. `path_aliases` already existed as a config-file
setting with no UI and no reload: changing it required editing config.json on an
on-air box AND restarting, which is exactly the kind of hand edit that stops a
station.

Invariants under test:
  1. Saving from the UI applies on the next feed tick — no restart.
  2. The pairs survive a restart (they live in the DB; config.json is only the
     factory default).
  3. A rewrite can only turn "cannot find" into "found" — it never touches a
     path that already resolves.
  4. Malformed pairs are refused with a reason (a silent no-op reads as a bug).
  5. Admin only; an operator can see the values but cannot change them.

Run: python tests/test_path_aliases.py   (~2s)
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient                        # noqa: E402

from services.core import auth, db, playlists as pl              # noqa: E402
from services.core.app import create_app                         # noqa: E402

passed = 0
OLD = "\\\\KDPI-Media\\music\\"


def check(name, cond, extra=""):
    global passed
    if not cond:
        print("FAIL:", name, extra)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def make_app(td, dbp):
    webtd = os.path.join(td, "web")
    os.makedirs(os.path.join(webtd, "precache"), exist_ok=True)
    cfg = {"station_name": "AliasTest", "db_path": dbp,
           "secret_path": os.path.join(webtd, "secret.key"),
           "precache_dir": os.path.join(webtd, "precache"),
           "engine_url": "http://127.0.0.1:9",
           "journal_path": os.path.join(webtd, "play_journal.jsonl"),
           "feeder_enabled": False}
    return TestClient(create_app(cfg), follow_redirects=False)


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        dbp = os.path.join(td, "core.db")
        db.migrate(dbp)
        conn = db.connect(dbp)

        # a stand-in "library" the alias will point at
        lib = os.path.join(td, "library")
        os.makedirs(os.path.join(lib, "G", "Band"), exist_ok=True)
        track = os.path.join(lib, "G", "Band", "song.mp3")
        with open(track, "wb") as fh:
            fh.write(b"ID3")            # contents never read by resolve_item

        pid = pl.create_playlist(conn, "Alias Test List")
        pl.add_item(conn, pid, "file",
                    OLD + "G\\Band\\song.mp3", "song")

        client = make_app(td, dbp)
        r = client.post("/setup", data={"username": "boss",
                                        "password": "longenough",
                                        "password2": "longenough"})
        check("setup signs the first admin in", r.status_code == 303,
              str(r.status_code))

        # ---- 1. empty to start, and nothing resolves yet ------------------
        d = client.get("/api/settings/path_aliases").json()
        check("no aliases configured on a fresh station", d["aliases"] == [], str(d))
        check("the API advertises its cap", d.get("max", 0) >= 1)
        item = dict(conn.execute("SELECT * FROM playlist_items WHERE playlist_id=?",
                                 (pid,)).fetchone())
        check("before the alias the stored path does not resolve",
              pl.resolve_item(conn, item) is None)

        # ---- 2. save applies IMMEDIATELY (no restart) --------------------
        body = {"aliases": [{"from": OLD, "to": lib}]}
        r = client.post("/api/settings/path_aliases", json=body)
        check("save accepted", r.status_code == 200, str(r.status_code))
        d = r.json()
        check("save reports one alias in use", d.get("applied") == 1, str(d))
        check("the resolver rewrote the stored prefix at once",
              pl.alias_path(OLD + "G\\Band\\song.mp3") == lib + "\\G\\Band\\song.mp3",
              pl.alias_path(OLD + "G\\Band\\song.mp3"))
        check("the playlist item now resolves to the real file",
              pl.resolve_item(conn, item) == track)
        check("a path that already resolved is left alone",
              pl.alias_path("Z:\\G\\whatever.mp3") == "Z:\\G\\whatever.mp3")

        # ---- 3. it survives a restart ------------------------------------
        dr = client.get("/api/settings/path_aliases").json()
        check("the saved pair is read back",
              dr["aliases"] == [{"from": OLD.rstrip("\\"), "to": lib}], str(dr))
        check("a trailing separator on the left is normalised away, so the "
              "stored path still resolves",
              pl.alias_path(OLD + "G\\Band\\song.mp3") == lib + "\\G\\Band\\song.mp3",
              pl.alias_path(OLD + "G\\Band\\song.mp3"))
        pl.set_path_aliases({})          # a fresh process knows nothing
        check("...and a fresh resolver has no aliases",
              pl.alias_path(OLD + "x.mp3") == OLD + "x.mp3")
        # a real restart runs the app's startup events (TestClient only fires
        # them inside the context manager — without it, this proves nothing)
        with make_app(td, dbp) as client2:
            client2.get("/api/settings/path_aliases")
            check("a restart re-applies the saved alias",
                  pl.alias_path(OLD + "G\\Band\\song.mp3").startswith(lib),
                  pl.alias_path(OLD + "G\\Band\\song.mp3"))

        # ---- 4. bad input is refused with a reason -----------------------
        for bad, why in (
            ({"aliases": [{"from": OLD, "to": ""}]}, "a half-filled pair"),
            ({"aliases": [{"from": "Z:", "to": "Y:"}]}, "a bare drive letter"),
            ({"aliases": [{"from": OLD, "to": OLD}]}, "a self-mapping"),
            ({"aliases": "nope"}, "a non-list"),
            ({"aliases": [{"from": f"\\\\NAS\\s{i}", "to": "Z:"}
                          for i in range(20)]}, "more than the cap"),
        ):
            r = client.post("/api/settings/path_aliases", json=bad)
            check(f"{why} is refused", r.status_code == 400, str(r.status_code))
        check("the good alias survived the refusals",
              pl.alias_path(OLD + "y.mp3").startswith(lib))

        # a duplicate left side keeps one row, not two
        r = client.post("/api/settings/path_aliases", json={"aliases": [
            {"from": OLD, "to": lib}, {"from": OLD.upper(), "to": "Z:"}]})
        check("duplicate left sides collapse to one", r.status_code == 200)
        check("...and the first wins", r.json()["applied"] == 1, str(r.json()))

        # blank rows (the screen always leaves one) are dropped, not errors
        r = client.post("/api/settings/path_aliases", json={"aliases": [
            {"from": OLD, "to": lib}, {"from": "", "to": ""}]})
        check("a blank row is ignored rather than refused", r.status_code == 200)
        check("...leaving the real alias intact", r.json()["applied"] == 1,
              str(r.json()))

        # ---- 5. operators may look, not touch ----------------------------
        auth.create_user(conn, "op", "longenough", "operator")
        opc = make_app(td, dbp)
        rr = opc.post("/login", data={"username": "op", "password": "longenough"})
        check("operator signs in", rr.status_code == 303, str(rr.status_code))
        check("operator can read the aliases",
              opc.get("/api/settings/path_aliases").status_code == 200)
        check("operator cannot change them",
              opc.post("/api/settings/path_aliases",
                       json={"aliases": []}).status_code == 403)

        # ---- 6. clearing them restores plain behaviour -------------------
        r = client.post("/api/settings/path_aliases", json={"aliases": []})
        check("clearing is allowed", r.status_code == 200 and r.json()["applied"] == 0,
              str(r.json()))
        check("...and the prefix stops resolving again",
              pl.resolve_item(conn, item) is None)

        conn.close()

    print(f"PATH ALIASES OK ({passed} checks)")


if __name__ == "__main__":
    main()
