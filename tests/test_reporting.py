"""Reporting: attribution capture + the three reports.

Proves the chain that makes the two new reports trustworthy:

  feeder feeds an item  -> queue_attribution(path -> playlist/spot ids)
  engine journals it    -> ingest copies the ids into play_history.extra
  report                -> "which playlist aired from when" and "did this spot air"

Run: python tests/test_reporting.py
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db, playlists as pl, reports, spots           # noqa: E402
from services.core.engine_bridge import (                                # noqa: E402
    close_airing, ingest_journal, open_airing, record_attribution)

passed = 0


def check(name, cond, extra=""):
    global passed
    if not cond:
        print("FAIL:", name, extra)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        dbp = os.path.join(td, "core.db")
        db.migrate(dbp)
        conn = db.connect(dbp)

        # ---- a playlist with two songs, and a spot rule -------------------
        pid = pl.create_playlist(conn, "MUSIC LIST 10")
        pl.add_items_bulk(conn, pid, [
            {"item_type": "file", "path": "Z:\\G\\A\\01 Song One.mp3",
             "title": "Song One"},
            {"item_type": "file", "path": "Z:\\G\\B\\02 Song Two.mp3",
             "title": "Song Two"},
        ])
        items = pl.get_items(conn, pid)
        check("playlist stored", len(items) == 2, str(len(items)))

        db.set_setting(conn, "dir_ads", "Z:\\1 - UNDERWRITING")
        rule = spots.add(conn, "", "interval", interval_min=30,
                         folder_path="Z:\\1 - UNDERWRITING")
        check("underwriting rule is protected policy",
              spots.rule_kind(conn, spots.get(conn, rule)) == "ads")

        # ---- 1. attribution is recorded against the precache path --------
        p1 = os.path.join(td, "precache", "aaa111.mp3")
        p2 = os.path.join(td, "precache", "bbb222.mp3")
        ps = os.path.join(td, "precache", "ccc333.mp3")
        os.makedirs(os.path.dirname(p1), exist_ok=True)
        record_attribution(conn, p1, "playlist", playlist_id=pid,
                           item_id=items[0]["id"], label="Song One")
        record_attribution(conn, p2, "playlist", playlist_id=pid,
                           item_id=items[1]["id"], label="Song Two")
        record_attribution(conn, ps, "spot", spot_rule_id=rule,
                           label="1 - UNDERWRITING (random)")
        rows = conn.execute("SELECT * FROM queue_attribution").fetchall()
        check("attribution rows recorded", len(rows) == 3, str(len(rows)))

        # ---- 2. the airing window is open/closed by the feeder -----------
        open_airing(conn, pid, "MUSIC LIST 10", "activate")
        # backdate the stretch a few minutes so the journal rows written below
        # fall INSIDE it (a same-second window is a race, not a scenario)
        conn.execute("UPDATE playlist_airings SET started_at = ? "
                     "WHERE ended_at IS NULL", (time.time() - 300,))
        conn.commit()

        # ---- 3. the journal ingest copies attribution into play_history --
        journal = os.path.join(td, "play_journal.jsonl")
        base = time.time()
        lines = []
        for i, (path, title) in enumerate((
                (p1, "A Band - Song One"),
                (ps, "1 - UNDERWRITING (random): credit.mp3"),
                (p2, "B Band - Song Two"))):
            # the engine writes local ISO timestamps WITH an offset — mimic that,
            # a naive timestamp is read as UTC and falls outside the airing
            ts = __import__("datetime").datetime.fromtimestamp(
                base - 240 + i * 60).astimezone().isoformat()
            lines.append({"id": "%d-%d" % (int(base) + i, i), "ts": ts,
                          "event": "track_start", "path": path, "title": title,
                          "source": "spot" if path == ps else "playlist"})
        with open(journal, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        ingest_journal(conn, journal)
        got = [dict(r) for r in conn.execute(
            "SELECT title, extra FROM play_history WHERE event='track_start' "
            "ORDER BY id").fetchall()]
        check("journal ingested", len(got) == 3, str(len(got)))
        ex0 = json.loads(got[0]["extra"] or "{}")
        check("play history carries the playlist attribution",
              ex0.get("playlist_id") == pid and ex0.get("attr_kind") == "playlist",
              str(ex0))
        ex1 = json.loads(got[1]["extra"] or "{}")
        check("play history carries the spot rule id",
              ex1.get("spot_rule_id") == rule and ex1.get("attr_kind") == "spot",
              str(ex1))

        close_airing(conn, "switched")
        check("airing closed",
              conn.execute("SELECT COUNT(*) FROM playlist_airings "
                           "WHERE ended_at IS NOT NULL").fetchone()[0] == 1)

        # ---- 4. the playlist-airings report ------------------------------
        today = time.strftime("%Y-%m-%d")
        rep = reports.run("playlist-airings", conn, today, today)
        check("playlist report has a row", len(rep["rows"]) == 1, str(rep["rows"]))
        row = rep["rows"][0]
        check("report names the playlist", row["playlist"] == "MUSIC LIST 10",
              row["playlist"])
        check("report counts the tracks that aired", row["tracks"] == 2,
              str(row["tracks"]))
        check("report shows a start and an end",
              row["started"] and row["ended"] != "", str(row))

        # an unfiltered day with no row must not invent history
        rep2 = reports.run("playlist-airings", conn, "2020-01-01", "2020-01-01")
        check("no airings => no rows (never invented)", rep2["rows"] == [])

        # ---- 5. the spot-airings report (expected vs actual) --------------
        # the interval rule owes 48/day; one airing was ingested above
        rep3 = reports.run("spot-airings", conn, today, today)
        check("spot report lists the rule", len(rep3["rows"]) == 1)
        srow = rep3["rows"][0]
        check("expected airings computed from the trigger",
              srow["expected"] == 48, str(srow["expected"]))
        check("actual airings counted", srow["aired"] == 1, str(srow["aired"]))
        check("shortfall reported", srow["missed"] == 47, str(srow["missed"]))
        check("status says UNDER", srow["status"] == "UNDER", srow["status"])
        check("type column names underwriting", srow["kind"] == "Underwriting",
              srow["kind"])

        # a paused rule owes nothing
        spots.set_enabled(conn, rule, False)
        rep4 = reports.run("spot-airings", conn, today, today)
        check("paused rule expects nothing",
              rep4["rows"][0]["expected"] == 0, str(rep4["rows"][0]["expected"]))
        spots.set_enabled(conn, rule, True)

        # ---- 6. as-aired + CSV -------------------------------------------
        rep5 = reports.run("as-aired", conn, today, today, kind="all")
        check("as-aired lists the three events", len(rep5["rows"]) == 3,
              str(len(rep5["rows"])))
        check("as-aired types the spot separately",
              [r["type"] for r in rep5["rows"]].count("spot") == 1,
              str([r["type"] for r in rep5["rows"]]))
        csv_text = reports.to_csv(rep5)
        check("CSV has a BOM for Excel", csv_text.startswith("\ufeff"))
        check("CSV carries the header row", "When,What aired,Type" in csv_text,
              csv_text[:120])

        # ---- 7. bad input is refused, big ranges are clamped -------------
        try:
            reports.run("nope", conn, today, today)
            check("unknown report rejected", False)
        except ValueError:
            check("unknown report rejected", True)

        # ---- 8. a SHOW airing that plays real playlist items still counts ----
        # 2026-11-19 on the on-air box: a one-shot show airing stayed open for
        # hours, so every rotation song inside it was stamped source='show'.
        # The music reports filtered on source='playlist' alone and read EMPTY
        # for the whole day (359 plays, 0 of them 'playlist'). A play that
        # carries a real item_id is a library airing whatever the airing label
        # says; a whole pre-recorded show file (no item_id) must still not be
        # counted as a per-song airing.
        db2 = os.path.join(td, "show_attribution.db")
        db.migrate(db2)
        c2 = db.connect(db2)

        pid2 = pl.create_playlist(c2, "SHOW NIGHT")
        pl.add_items_bulk(c2, pid2, [
            {"item_type": "file", "path": "Z:\\G\\S\\01 Show Song.mp3",
             "title": "Show Song"},
            {"item_type": "file", "path": "Z:\\G\\S\\Dead Air Show.mp3",
             "title": "Dead Air Show"},
        ])
        items2 = pl.get_items(c2, pid2)
        for path, artist, genre in ((items2[0]["path"], "A Band", "Rock"),
                                    (items2[1]["path"], "Dead Air", "Spoken")):
            c2.execute("INSERT INTO tracks (path, title, artist, album, genre,"
                       " indexed_at, missing, tags_read, meta_read)"
                       " VALUES (?,?,?,?,?,?,0,1,1)",
                       (path, os.path.basename(path), artist, "An Album",
                        genre, time.time()))
        c2.commit()

        # the feeder feeds ONE of them from the playlist; the show file is
        # played whole, so it gets no per-item attribution
        pa = os.path.join(td, "precache", "eee555.mp3")
        pb = os.path.join(td, "precache", "fff666.mp3")
        pc = os.path.join(td, "precache", "ggg777.mp3")
        record_attribution(c2, pa, "playlist", playlist_id=pid2,
                           item_id=items2[0]["id"], label="Show Song",
                           src_path=items2[0]["path"])
        # a .lst/folder show item: synthetic id, no playlist_items row at all —
        # the source path is the only way back to the track row
        c2.execute("INSERT INTO tracks (path, title, artist, album, genre,"
                   " indexed_at, missing, tags_read, meta_read)"
                   " VALUES (?,?,?,?,?,?,0,1,1)",
                   ("Z:\\G\\S\\03 List Song.mp3", "03 List Song", "C Band",
                    "An Album", "Blues", time.time()))
        c2.commit()
        record_attribution(c2, pc, "playlist", playlist_id=pid2,
                           item_id="l7", label="C Band - List Song",
                           src_path="Z:\\G\\S\\03 List Song.mp3")

        open_airing(c2, None, "December Playlist #3", "show")
        c2.execute("UPDATE playlist_airings SET started_at = ? "
                   "WHERE ended_at IS NULL", (time.time() - 300,))
        c2.commit()

        journal3 = os.path.join(td, "play_journal_show.jsonl")
        base3 = time.time()
        with open(journal3, "w", encoding="utf-8") as f:
            for i, (path, title) in enumerate(((pa, "A Band - Show Song"),
                                               (pb, "Dead Air Show"),
                                               (pc, "C Band - List Song"))):
                ts = __import__("datetime").datetime.fromtimestamp(
                    base3 - 120 + i * 30).astimezone().isoformat()
                f.write(json.dumps({"id": "show-%d" % i, "ts": ts,
                                    "event": "track_start", "path": path,
                                    "title": title, "source": "show"}) + "\n")
        ingest_journal(c2, journal3)
        shows = [dict(r) for r in c2.execute(
            "SELECT title, source, extra FROM play_history "
            "WHERE event='track_start' AND source='show' ORDER BY id")]
        check("show plays ingested", len(shows) == 3, str(len(shows)))
        ex_a = json.loads(shows[0]["extra"] or "{}")
        check("show play of a playlist item keeps its item_id",
              ex_a.get("item_id") == items2[0]["id"], str(ex_a))
        check("show play of a playlist item carries its source path",
              ex_a.get("src_path") == items2[0]["path"], str(ex_a))
        ex_b = json.loads(shows[1]["extra"] or "{}")
        check("whole-show play has no item_id", ex_b.get("item_id") is None,
              str(ex_b))
        check("whole-show play has no source path",
              ex_b.get("src_path") is None, str(ex_b))
        ex_c = json.loads(shows[2]["extra"] or "{}")
        check("lst show item keeps its synthetic id and source path",
              ex_c.get("item_id") == "l7"
              and ex_c.get("src_path") == "Z:\\G\\S\\03 List Song.mp3",
              str(ex_c))

        census = reports.run("play-census", c2, today, today)
        check("census counts the songs aired inside a show",
              sorted(r["track"] for r in census["rows"])
              == ["A Band - Show Song", "C Band - List Song"],
              str(census["rows"]))
        check("census still ignores a whole-show file",
              all("Dead Air Show" not in r["track"] for r in census["rows"]),
              str(census["rows"]))

        mix = reports.run("genre-mix", c2, today, today)
        check("genre mix counts the songs aired inside a show",
              {r["genre"]: r["plays"] for r in mix["rows"]}
              == {"Rock": 1, "Blues": 1},
              str(mix["rows"]))
        check("genre coverage counts them as attributable plays",
              mix["coverage"]["total"] == 2, str(mix["coverage"]))
        check("genre chart is not empty",
              any(c["title"].startswith("Plays by genre") for c in mix["charts"]),
              str([c["title"] for c in mix["charts"]]))

        art = reports.run("artist-spins", c2, today, today)
        check("artist airplay counts the songs aired inside a show",
              {r["artist"]: r["spins"] for r in art["rows"]}
              == {"A Band": 1, "C Band": 1},
              str(art["rows"]))

        c2.close()

        conn.close()

    print("\nREPORTING OK (%d checks)" % passed)


if __name__ == "__main__":
    main()
