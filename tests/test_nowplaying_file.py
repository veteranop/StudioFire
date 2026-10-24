"""Tests for the now-playing text file (#747) — the BUTT handoff.

Covers the things that would silently break a live station:
  * the atomic write (no temp file left behind, correct content + newline)
  * ASCII folding, because Icecast mangles UTF-8 downstream
  * never raising on a missing / unwritable path (feeder thread must survive)
  * choosing the CURRENT track out of a backlog, and ignoring spots
  * the DB setting round-trip and the save-time write probe

Run: python tests/test_nowplaying_file.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db, nowplaying_file, streaming   # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def fresh_conn():
    tmp = tempfile.mkdtemp(prefix="sf-npf-")
    dbp = os.path.join(tmp, "t.db")
    db.migrate(dbp)
    return db.connect(dbp), tmp


def main():
    conn, tmp = fresh_conn()

    # -- folding ------------------------------------------------------------
    check("fold strips an accent",
          nowplaying_file.fold("Björk – Jóga") == "Bjork - Joga")
    check("fold keeps a plain title intact",
          nowplaying_file.fold("Alice Cooper - No More Mr. Nice Guy")
          == "Alice Cooper - No More Mr. Nice Guy")

    # -- choosing the on-air track -----------------------------------------
    backlog = [
        {"id": "1000-1", "path": "a.mp3", "title": "Old Song", "source": "show"},
        {"id": "1000-9", "path": "c.mp3", "title": "Current Song",
         "source": "show"},
    ]
    ev = nowplaying_file.choose(backlog, "a.mp3")
    check("choose prefers the track the engine says is on air",
          ev["title"] == "Old Song")
    ev = nowplaying_file.choose(backlog, None)
    check("choose falls back to the newest event", ev["title"] == "Current Song")
    ev = nowplaying_file.choose(
        [{"id": "1-1", "path": "x.mp3", "title": "Underwriter Spot",
          "source": "spot"}], None)
    check("choose ignores spots (a listener keeps seeing the song)", ev is None)
    ev = nowplaying_file.choose(
        [{"id": "1-1", "path": "x.mp3", "title": None, "source": "playlist"}],
        None)
    check("choose ignores an empty title", ev is None)

    # -- the write itself ---------------------------------------------------
    target = os.path.join(tmp, "Current", "CurrentSong.txt")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    ok = nowplaying_file.write_text(target, "The Byrds - Chestnut Mare")
    check("write_text reports success", ok is True)
    with open(target, encoding="ascii") as fh:
        body = fh.read()
    check("file holds the title with a trailing newline",
          body == "The Byrds - Chestnut Mare\n")
    check("no temp file is left behind",
          [f for f in os.listdir(os.path.dirname(target))
           if f.endswith(".tmp")] == [])
    nowplaying_file.write_text(target, "Badfinger - Maybe Tomorrow")
    with open(target, encoding="ascii") as fh:
        check("a second write replaces the first", fh.read().startswith("Badfinger"))

    check("empty path is refused, not crashed",
          nowplaying_file.write_text("", "Anything") is False)
    bad = nowplaying_file.write_text(r"\\192.0.2.1\does\not\exist\x.txt", "X")
    check("an unreachable network path fails soft", bad is False)
    check("the failure is recorded for the monitor",
          nowplaying_file.status()["ok"] is False
          and nowplaying_file.status()["error"])

    # -- the DB setting + the feeder entry point ---------------------------
    check("path setting starts empty", nowplaying_file.get_path(conn) == "")
    nowplaying_file.set_path(conn, target)
    check("path setting round-trips", nowplaying_file.get_path(conn) == target)

    wrote = nowplaying_file.write_now_playing(
        conn, [{"id": "2000-1", "title": "Echo & The Bunnymen - Seven Seas",
                "source": "show"}], None)
    check("write_now_playing writes the ingested title", wrote is True)
    with open(target, encoding="ascii") as fh:
        check("...and the content is the right song",
              "Seven Seas" in fh.read())

    check("a spot event does not overwrite the file",
          nowplaying_file.write_now_playing(
              conn, [{"id": "2000-2", "title": "PSA", "source": "spot"}], None)
          is False)

    # -- startup recovery ---------------------------------------------------
    conn.execute(
        "INSERT INTO play_history (journal_id, ts, event, path, title, source) "
        "VALUES ('x-1','2026-10-24T10:00:00-06:00','track_start','p.mp3',"
        "'Robert Plant - 29 Palms','show')")
    conn.commit()
    check("write_current recovers the last aired song at startup",
          nowplaying_file.write_current(conn, {}) is True)
    with open(target, encoding="ascii") as fh:
        check("...and the file shows it", "29 Palms" in fh.read())

    # -- save-time probe ----------------------------------------------------
    ok, err = streaming._probe_file(target)
    check("probe accepts a writable target", ok is True and err is None)
    ok, err = streaming._probe_file(r"\\192.0.2.1\nope\x.txt")
    check("probe rejects an unwritable target", ok is False and err)
    ok, err = streaming._probe_file("")
    check("probe accepts clearing the setting", ok is True)

    # -- status exposure ----------------------------------------------------
    st = streaming.read_settings(conn)
    check("Settings exposes the file path", st["np_file"] == target)
    check("status() carries the last write for the monitor",
          nowplaying_file.status()["title"])

    print(f"\nPASS {passed} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
