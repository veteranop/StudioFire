"""Tests for the multi-field now-playing line (#926) — the stream vendor's format.

StreamGuys builds the SoundExchange report of use straight off the ICY
StreamTitle we publish, so this line is a FORMAT CONTRACT, not cosmetics.
What would hurt a live station if it broke:

  * field ORDER drifting between songs (their parser keys on position)
  * a missing value collapsing instead of keeping its empty slot
  * a `|` inside an artist or title shifting every column after it
  * the joined string being folded differently from the legacy line
  * a template typo shipping literal text to the public player
  * the writer raising in the feeder thread because a tag read failed

Run: python tests/test_nowplaying_format.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db, nowplaying_file, streaming, tagfields   # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def fresh_conn():
    tmp = tempfile.mkdtemp(prefix="sf-npfmt-")
    dbp = os.path.join(tmp, "t.db")
    db.migrate(dbp)
    return db.connect(dbp), tmp


def ev(**kw):
    base = {"id": "1000-1", "path": "", "source": "show"}
    base.update(kw)
    return base


def main():
    conn, tmp = fresh_conn()

    # -- the vendor's shape -------------------------------------------------
    line = nowplaying_file.render(ev(title="Robert Pollard - Gold", artist="Robert Pollard",
                                     song="Gold", album="From A Compound Eye"))
    check("default format is the vendor's field order",
          nowplaying_file.DEFAULT_FORMAT ==
          "{artist} | {title} | {album} | {label} | {isrc}")
    check("renders artist | title | album with empty label/isrc slots",
          line == "Robert Pollard | Gold | From A Compound Eye | |")
    check("a missing ISRC keeps a trailing empty slot, never a dropped pipe",
          line.count("|") == 4 and line.endswith("|"))

    # -- separator anatomy: every song has the same column count ------------
    for title, expect_pipes in (("A - B", 4), ("A - B - C", 4)):
        l2 = nowplaying_file.render(ev(title=title))
        check(f"'{title}' still yields 4 delimiters ({expect_pipes})",
              l2.count("|") == expect_pipes)

    # -- interior pipes are sanitised, not escaped --------------------------
    l3 = nowplaying_file.render(ev(title="X - Y", artist="A|B",
                                   song="Take Your Time | Shine A Light",
                                   album="D|E"))
    check("an interior pipe in the artist is replaced", "A/B" in l3 and "A|B" not in l3)
    check("an interior pipe in the title is replaced", "Take Your Time / Shine A Light" in l3)
    check("an interior pipe in the album is replaced", "D/E" in l3)
    check("sanitising leaves exactly the 4 structural delimiters", l3.count("|") == 4)

    # -- what the station actually airs -------------------------------------
    l4 = nowplaying_file.render(ev(title="Fleetwood Mac - Oh Well - Pt. 2"))
    check("a dash inside a real title survives (structured fields, not dash parsing)",
          l4 == "Fleetwood Mac | Oh Well - Pt. 2 | | |")

    # -- event without separate fields --------------------------------------
    l5 = nowplaying_file.render(ev(title="Talking Heads \u2013 Once in a Lifetime"))
    check("an en-dash display title is split into artist and title",
          l5.startswith("Talking Heads | Once in a Lifetime |"))

    # -- no artist at all (the 2% of the library John's tags miss) ----------
    l6 = nowplaying_file.render(ev(title="01 Gold"))
    check("a title with no separator keeps the title and leaves artist empty",
          l6 == "| 01 Gold | | |")

    # -- folding still applies (UTF-8 is NOT proven in the vendor's CSV) ----
    accent_file = os.path.join(tmp, "accent.txt")
    nowplaying_file.set_path(conn, accent_file)
    nowplaying_file.set_format(conn, "")
    nowplaying_file.write_now_playing(
        conn, [ev(title="Bj\u00f6rk - J\u00f3ga", artist="Bj\u00f6rk",
                  song="J\u00f3ga", album="Homog\u00e9nic")], None)
    l7 = open(accent_file, encoding="ascii").read().strip()
    check("accented names are still ASCII-folded in the written file",
          l7 == "Bjork | Joga | Homogenic | |")
    check("the written file is pure ASCII (Icecast-safe)",
          all(ord(ch) < 128 for ch in l7))

    # -- template handling --------------------------------------------------
    check("a blank template means the default",
          nowplaying_file.validate_format("   ") == "")
    try:
        nowplaying_file.validate_format("{artist} | {titel}")
        check("a typo'd placeholder is rejected", False)
    except ValueError as exc:
        check("a typo'd placeholder is rejected", "titel" in str(exc))
    try:
        nowplaying_file.validate_format("plain text only")
        check("a template with no fields is rejected", False)
    except ValueError:
        check("a template with no fields is rejected", True)

    nowplaying_file.set_format(conn, nowplaying_file.FORMAT_LEGACY)
    check("the legacy template round-trips through the DB",
          nowplaying_file.get_format(conn) == "{artist} - {title}")
    l8 = nowplaying_file.render(ev(title="Robert Pollard - Gold", artist="Robert Pollard",
                                   song="Gold", album="From A Compound Eye"),
                                nowplaying_file.get_format(conn))
    check("the one-setting revert still produces the old line",
          l8 == "Robert Pollard - Gold")

    # -- never raises -------------------------------------------------------
    for junk in (None, {}, {"title": None}, {"title": 42}, {"title": "x", "path": 7}):
        try:
            nowplaying_file.render(junk)
        except Exception as exc:                                   # noqa: BLE001
            check(f"render survives {junk!r} ({exc})", False)
            break
    check("render never raises on junk input", True)

    check("tagfields.read on a missing path is empty, not an error",
          tagfields.read(os.path.join(tmp, "nope.mp3")) ==
          {"album": "", "label": "", "isrc": ""})
    junkfile = os.path.join(tmp, "x.bin")
    open(junkfile, "wb").write(b"not audio")
    check("tagfields.read on a non-audio file is empty, not an error",
          tagfields.read(junkfile)["isrc"] == "")

    # -- end to end: the feeder's write path -------------------------------
    target = os.path.join(tmp, "CurrentSong.txt")
    nowplaying_file.set_path(conn, target)
    nowplaying_file.set_format(conn, "")          # back to the default
    wrote = nowplaying_file.write_now_playing(
        conn, [ev(title="Robert Pollard - Gold", artist="Robert Pollard",
                  song="Gold", album="From A Compound Eye")], None)
    body = open(target, encoding="ascii").read().strip()
    check("write_now_playing writes the pipe line", wrote is True and body ==
          "Robert Pollard | Gold | From A Compound Eye | |")
    check("the file carries exactly one line",
          open(target, encoding="ascii").read().count("\n") == 1)

    # a startup re-write from play_history (no journal event) takes the same path
    conn.execute("INSERT INTO play_history (ts, title, path, source, event, "
                 "journal_id) VALUES (?,?,?,?,?,?)",
                 ("2026-10-30T20:00:00-06:00", "Robert Pollard - Gold",
                  "Z:/G/x.mp3", "show", "track_start", "t-1"))
    conn.commit()
    check("write_current renders the same format",
          nowplaying_file.write_current(conn) is True and
          open(target, encoding="ascii").read().strip().count("|") == 4)

    # -- Settings exposure --------------------------------------------------
    st = streaming.read_settings(conn)
    check("Settings exposes the format", st["np_format"] == nowplaying_file.DEFAULT_FORMAT)
    check("Settings still exposes the file path", st["np_file"] == target)

    print(f"\nPASS {passed} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
