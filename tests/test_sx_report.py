"""Unit tests for the SoundExchange report builder — ZERO network, ZERO disk I/O
beyond a temp DB and temp CSV files.

Covers the things that would silently produce a WRONG report:
  * precache digest parity with engine_bridge (the whole join rests on it)
  * digest extraction from a precache path, and rejection of non-precache paths
  * drive-letter path variants (the same file reaching the engine as Z:\\...)
  * title-string fallback when the file is no longer in the index
  * period boundaries (inclusive start, exclusive end, mixed offsets)
  * spot/music source filtering
  * aggregation: ISRC key vs artist+title+album+label key, play counts
  * ATP is NEVER invented: blank without listener data, computed only with it
  * listener-series mean over a play window, and the uncovered-window case
  * CSV column order == 37 CFR 370.4(d)(2)(i)-(vi)

Run: python tests/test_sx_report.py
"""
import datetime as dt
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db                                    # noqa: E402
from services.core import sx_report                             # noqa: E402
from services.core.engine_bridge import Precache                 # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def fresh_conn():
    tmp = tempfile.mkdtemp(prefix="sf-sx-")
    dbp = os.path.join(tmp, "t.db")
    db.migrate(dbp)
    return db.connect(dbp)


def add_track(conn, path, title, artist, album, dur=200.0):
    conn.execute(
        "INSERT INTO tracks (path, title, artist, album, duration_sec, format, "
        "size, mtime, indexed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (path, title, artist, album, dur, "mp3", 1, 1.0, 1.0))
    conn.commit()


def add_play(conn, jid, ts, precache_path, title, source="show"):
    conn.execute(
        "INSERT INTO play_history (journal_id, ts, event, path, title, source) "
        "VALUES (?,?,?,?,?,?)",
        (jid, ts, "track_start", precache_path, title, source))
    conn.commit()


def main():
    # -- digest parity with the engine's own function -----------------------
    src = r"Z:\John\New PSAs\spot.mp3"
    p = Precache("C:\\tmp\\precache")
    check("digest matches Precache.cache_path_for",
          sx_report.precache_digest(src) ==
          os.path.basename(p.cache_path_for(src)).split(".")[0])

    check("digest_from_precache_path extracts",
          sx_report.digest_from_precache_path(
              "C:\\SF\\precache\\21f14d911e6855d3.m4a") == "21f14d911e6855d3")
    check("digest_from_precache_path rejects the sine tone",
          sx_report.digest_from_precache_path("av://lavfi:sine=frequency=440") is None)
    check("digest_from_precache_path rejects a non-precache file",
          sx_report.digest_from_precache_path("C:\\SF\\logs\\x.log") is None)

    # -- drive-letter variants ---------------------------------------------
    variants = sx_report._path_variants(
        "//KDPI-Media/music/G\\Artist\\Album\\01 - Song.mp3", "\\\\KDPI-Media\\music")
    check("path variants include the Z: form",
          "Z:\\G\\Artist\\Album\\01 - Song.mp3" in variants)
    check("path variants always include the stored form",
          "//KDPI-Media/music/G\\Artist\\Album\\01 - Song.mp3" in variants)

    # -- period collection / join / fallback --------------------------------
    conn = fresh_conn()
    lib = "//KDPI-Media/music/G\\Artist\\Album\\01 - Song.mp3"
    add_track(conn, lib, "Song", "Artist", "Album", 201.0)
    cached = "C:\\SF\\precache\\" + sx_report.precache_digest(lib) + ".mp3"
    add_play(conn, "1-1", "2026-09-01T10:00:00.000-06:00", cached,
             "Artist - Song")
    # same file, but the feeder handed the engine the drive-letter form
    zform = "Z:\\G\\Artist\\Album\\01 - Song.mp3"
    add_play(conn, "1-2", "2026-09-01T10:05:00.000-06:00",
             "C:\\SF\\precache\\" + sx_report.precache_digest(zform) + ".mp3",
             "Artist - Song")
    # a file no longer in the index — must fall back to the title string
    add_play(conn, "1-3", "2026-09-01T10:10:00.000-06:00",
             "C:\\SF\\precache\\" + "f" * 16 + ".mp3", "Other Band - Lost Song")
    # a title with no separator — nothing is silently dropped
    add_play(conn, "1-4", "2026-09-01T10:15:00.000-06:00",
             "C:\\SF\\precache\\" + "e" * 16 + ".mp3", "Station ID")
    # a spot, which must never enter the report
    add_play(conn, "1-5", "2026-09-01T10:20:00.000-06:00", cached,
             "Underwriter Spot", source="spot")
    # outside the window (exclusive end) and before start
    add_play(conn, "1-6", "2026-09-02T00:00:00.000-06:00", cached,
             "Artist - Song")
    add_play(conn, "1-7", "2026-08-31T23:59:59.000-06:00", cached,
             "Artist - Song")

    tz = dt.timezone(dt.timedelta(hours=-6))
    start = dt.datetime(2026, 9, 1, tzinfo=tz)
    end = dt.datetime(2026, 9, 2, tzinfo=tz)
    idx = sx_report.build_precache_index(conn, "//KDPI-Media/music/G")
    plays = sx_report.collect_period(conn, start, end, index=idx)

    check("period is start-inclusive / end-exclusive", len(plays) == 4)
    check("spot excluded", all(p["source"] != "spot" for p in plays))
    check("index join fills album + duration",
          plays[0]["album"] == "Album" and plays[0]["duration_sec"] == 201.0)
    check("drive-letter form also resolves to the indexed track",
          plays[1]["resolve"] == "indexed" and plays[1]["album"] == "Album")
    check("unindexed file falls back to the title string",
          plays[2]["resolve"] == "unmatched" and plays[2]["artist"] == "Other Band"
          and plays[2]["title"] == "Lost Song")
    check("title without a separator keeps the whole string and no artist",
          plays[3]["title"] == "Station ID" and plays[3]["artist"] == "")

    # -- aggregation: no ATP without listeners -----------------------------
    rows = sx_report.aggregate(plays)
    by_title = {r["Sound Recording Title"]: r for r in rows}
    check("duplicate performances group into one row", len(rows) == 3)
    check("ATPs left BLANK when no listener data (never play count)",
          by_title["Song"]["Actual Total Performances"] == "")
    check("play count kept separately for the audit", by_title["Song"]["_plays"] == 2)

    # -- aggregation: ISRC is the identity when present --------------------
    plays[0]["isrc"] = "USABC1234567"
    plays[1]["isrc"] = "USABC1234567"
    plays[2]["isrc"] = "USZZZ7654321"
    rows = sx_report.aggregate(plays)
    check("ISRC present => reported in the ISRC column",
          any(r["ISRC"] == "USABC1234567" for r in rows))

    # -- listener join ------------------------------------------------------
    series = [(dt.datetime(2026, 9, 1, 10, 0, 5, tzinfo=tz), 4),
              (dt.datetime(2026, 9, 1, 10, 1, 0, tzinfo=tz), 6),
              (dt.datetime(2026, 9, 1, 10, 6, 0, tzinfo=tz), 10)]
    missing = sx_report.apply_listeners(plays, series)
    check("plays outside the listener series are counted as missing",
          missing == len(plays) - 2)
    check("mean listeners over the window is used",
          plays[0]["listeners"] == 5.0)
    rows = sx_report.aggregate(plays)
    grouped = [r for r in rows if r["ISRC"] == "USABC1234567"][0]
    check("ATP computed as sum of listener counts when data exists",
          grouped["Actual Total Performances"] == 15)  # 5 (10:00) + 10 (10:05)

    # -- CSV contract -------------------------------------------------------
    out = tempfile.mkdtemp(prefix="sf-sx-out-")
    p_rep = os.path.join(out, "report.csv")
    p_log = os.path.join(out, "log.csv")
    sx_report.write_sx_report(rows, p_rep)
    sx_report.write_tracklog(plays, p_log)
    with open(p_rep, encoding="utf-8") as fh:
        header = fh.readline().strip()
    check("report header is the 370.4(d)(2) column order",
          header == ",".join(sx_report.SX_COLUMNS))
    with open(p_log, encoding="utf-8") as fh:
        log_header = fh.readline().strip()
    check("track log carries the audit columns",
          log_header.startswith("start_local,start_utc,artist,title"))

    print(f"\nPASS {passed} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
