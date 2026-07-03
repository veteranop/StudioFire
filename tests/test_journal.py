"""Unit tests: journal append, ids, rotation, torn-line tolerance.

Run: python tests/test_journal.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.engine.journal import Journal, read_events  # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def main():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "play_journal.jsonl")

        j = Journal(path)
        j.append("engine_start", version="0.1.0-dev")
        j.append("track_start", path="C:/x/a.mp3", title="Song A", source="playlist")
        j.append("track_end", path="C:/x/a.mp3", reason="eof", duration=183.2)
        j.close()

        events = list(read_events(path))
        check("three events persisted", len(events) == 3)
        check("event order", [e["event"] for e in events]
              == ["engine_start", "track_start", "track_end"])
        check("unique ids", len({e["id"] for e in events}) == 3)
        check("timestamps present", all("ts" in e for e in events))
        check("fields carried", events[1]["title"] == "Song A")

        # append survives reopen (restart) without clobbering history
        j = Journal(path)
        j.append("engine_start", version="0.1.0-dev")
        j.close()
        check("reopen appends, not truncates", len(list(read_events(path))) == 4)

        # torn last line (power loss mid-write) is skipped, rest read fine
        with open(path, "ab") as f:
            f.write(b'{"id": "torn", "event": "track_st')
        good = list(read_events(path))
        check("torn line skipped", len(good) == 4)

        # rotation
        j = Journal(path, max_bytes=2000)
        for i in range(50):
            j.append("track_start", path=f"C:/x/{i}.mp3", source="playlist")
        j.close()
        rotated = [f for f in os.listdir(td)
                   if f.startswith("play_journal.") and f != "play_journal.jsonl"]
        check("rotation happened", len(rotated) >= 1)
        total = len(list(read_events(path)))
        for r in rotated:
            total += len(list(read_events(os.path.join(td, r))))
        check("no events lost across rotation", total >= 54)  # 4 prior + 50 (torn skipped)

    print(f"JOURNAL OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
