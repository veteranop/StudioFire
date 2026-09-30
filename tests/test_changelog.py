"""Changelog parser + Settings "What's new" route.

Covers the parser (version order, group headings, JOINING wrapped bullets,
[Unreleased] handling, tolerance of a missing/garbage/empty file) and the
route (200 for a signed-in operator, includes the running VERSION). One case
asserts against the REAL repo CHANGELOG.md so a format change that breaks
parsing fails the suite.

Run: python tests/test_changelog.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient             # noqa: E402

from services.core import changelog                   # noqa: E402
from services.core.app import create_app              # noqa: E402
from services import updater                          # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


SAMPLE = """# Changelog

Some preamble text that isn't part of any release.

## [Unreleased]

### Added
- Something staged but not shipped.

## [1.2.0] - 2026-09-29

### Fixed
- **The station no longer needs a mapped drive.** A mapped drive only
  exists while someone is logged in, so after a reboot the station could
  come back up without it.
- A second, single-line fix.

### Changed
- The queue is shorter now.

## [1.1.0] - 2026-09-27

### Added (operator feedback from KDPI)
These came from John after the first live show.
- **Songs crossfade.** In the last 4 seconds the next song starts
  underneath it and rises.
"""


def test_parser():
    rels = changelog.parse(SAMPLE)

    # ---- versions, in file order (newest first), Unreleased flagged
    check("all versions parsed",
          [r["version"] for r in rels] == ["Unreleased", "1.2.0", "1.1.0"])
    check("Unreleased is not a release", rels[0]["released"] is False)
    check("real versions are releases",
          rels[1]["released"] and rels[2]["released"])
    released = [r for r in rels if r["released"]]
    check("released versions newest first",
          [r["version"] for r in released] == ["1.2.0", "1.1.0"])
    check("dates parsed off the '- ' separator",
          rels[1]["date"] == "2026-09-29" and rels[2]["date"] == "2026-09-27")

    v120 = rels[1]
    # ---- ### group headings kept, in order
    check("group headings kept",
          [g["heading"] for g in v120["groups"]] == ["Fixed", "Changed"])

    fixed = v120["groups"][0]
    # ---- wrapped bullets joined into ONE entry each
    check("Fixed has 2 entries (wrapped lines joined, not split)",
          len(fixed["entries"]) == 2)
    joined = fixed["entries"][0]
    check("wrapped bullet joined start-to-end",
          joined.startswith("**The station no longer needs a mapped drive.**")
          and joined.endswith("without it."))
    check("continuation joined with a single space, no newlines",
          "\n" not in joined and "  " not in joined
          and "logged in, so after" in joined)

    # ---- intro line between a ### heading and its bullets is captured, not
    #      swallowed into the first bullet
    added_11 = rels[2]["groups"][0]
    check("group heading with trailing text kept whole",
          added_11["heading"] == "Added (operator feedback from KDPI)")
    check("intro paragraph captured separately from the bullets",
          added_11["intro"] == "These came from John after the first live show."
          and len(added_11["entries"]) == 1)
    check("intro is not glued onto the first bullet",
          added_11["entries"][0].startswith("**Songs crossfade.**"))


def test_tolerance():
    # missing file -> not ok, but no exception
    miss = changelog.load(os.path.join(tempfile.gettempdir(), "no-such-dir-xyz"))
    check("missing file: ok=False, empty releases, no raise",
          miss["ok"] is False and miss["releases"] == [])

    td = tempfile.mkdtemp(prefix="sf-cl-")
    # empty file -> sane (no releases, ok=False)
    open(os.path.join(td, "CHANGELOG.md"), "w").close()
    empty = changelog.load(td)
    check("empty file: ok=False, releases=[]",
          empty["ok"] is False and empty["releases"] == [])

    # garbage / non-changelog text -> no raise, no released versions
    with open(os.path.join(td, "CHANGELOG.md"), "w", encoding="utf-8") as f:
        f.write("just some random text\nno headers at all\n### orphan heading\n")
    junk = changelog.load(td)
    check("garbage file tolerated (ok=False, no raise)", junk["ok"] is False)

    # a file with only [Unreleased] is not a released history
    with open(os.path.join(td, "CHANGELOG.md"), "w", encoding="utf-8") as f:
        f.write("## [Unreleased]\n### Added\n- soon\n")
    unrel = changelog.load(td)
    check("only-Unreleased file: ok=False (nothing released yet)",
          unrel["ok"] is False)


def test_bold_segments():
    segs = changelog.bold_segments("**Bold lead-in.** then plain text.")
    check("bold split into [text, is_bold] pairs",
          segs == [["Bold lead-in.", True], [" then plain text.", False]])
    # backslashes in paths must survive the segmenting untouched
    segs2 = changelog.bold_segments(r"maps a drive like Z:\ and \\KDPI-Media\music")
    check("backslashes preserved through segmenting",
          segs2 == [[r"maps a drive like Z:\ and \\KDPI-Media\music", False]])
    check("no bold -> single plain segment",
          changelog.bold_segments("plain") == [["plain", False]])


def test_real_repo_changelog():
    # Parse the ACTUAL shipped CHANGELOG.md — a format change that breaks the
    # parser fails here.
    cl = changelog.load(ROOT)
    check("real CHANGELOG parses ok", cl["ok"] is True and cl["releases"])
    released = [r for r in cl["releases"] if r["released"]]
    vers = [r["version"] for r in released]
    check("real releases include 1.2.0 / 1.1.0 / 1.0.0",
          {"1.2.0", "1.1.0", "1.0.0"} <= set(vers))
    check("real releases are newest-first",
          vers.index("1.2.0") < vers.index("1.1.0") < vers.index("1.0.0"))

    v120 = next(r for r in released if r["version"] == "1.2.0")
    check("real 1.2.0 has a Fixed group",
          any(g["heading"] == "Fixed" for g in v120["groups"]))
    fixed = next(g for g in v120["groups"] if g["heading"] == "Fixed")
    first = fixed["entries"][0]
    # the first 1.2.0 Fixed bullet is wrapped across ~10 source lines; the join
    # must span from its bold lead-in to its closing sentence, on one line.
    check("real wrapped bullet joined across many lines",
          first.startswith("**The station no longer needs a mapped drive")
          and "\n" not in first
          and "playlists, shows, IDs and PSAs keep working" in first)
    check("no released section is labelled Unreleased",
          all(r["version"].lower() != "unreleased" for r in released))


def test_route():
    td = tempfile.mkdtemp(prefix="sf-cl-route-")
    cfg = {"station_name": "TestFM",
           "db_path": os.path.join(td, "core.db"),
           "secret_path": os.path.join(td, "secret.key"),
           "precache_dir": os.path.join(td, "precache"),
           "data_dir": td,
           "nas_music_root": td,
           "engine_url": "http://127.0.0.1:1",
           "journal_path": os.path.join(td, "play_journal.jsonl"),
           "feeder_enabled": False}
    app = create_app(cfg)
    running = updater.read_version()          # from the repo VERSION file

    client = TestClient(app, follow_redirects=False)
    client.post("/setup", data={"username": "op", "password": "longenough",
                                "password2": "longenough"})

    # ---- Settings page renders the change log with the running version
    r = client.get("/settings")
    check("settings page renders the change log",
          r.status_code == 200 and b"What's new / Change log" in r.content)
    check("settings page shows the running version",
          ("StudioFire " + running).encode() in r.content)
    check("settings page shows a released version heading",
          b"Version 1.2.0" in r.content)

    # ---- JSON route: 200 for a signed-in operator, includes VERSION
    j = client.get("/api/changelog")
    check("changelog API returns 200 for an operator", j.status_code == 200)
    body = j.json()
    check("changelog API reports the running version",
          body["current"] == running)
    check("changelog API parsed ok with releases",
          body["ok"] is True and len(body["releases"]) >= 3)

    # ---- needs auth
    anon = TestClient(app, follow_redirects=False)
    check("changelog API needs auth (401 when signed out)",
          anon.get("/api/changelog").status_code == 401)
    check("settings page needs auth (303 when signed out)",
          anon.get("/settings").status_code == 303)


def main():
    test_parser()
    test_tolerance()
    test_bold_segments()
    test_real_repo_changelog()
    test_route()
    print(f"CHANGELOG OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
