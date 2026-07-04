"""P3 indexer tests: incremental scan semantics against a fake NAS tree.

Run: python tests/test_indexer.py
"""
import math
import os
import struct
import sys
import tempfile
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db                      # noqa: E402
from services.worker.indexer import scan          # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def make_wav(path, seconds=1.0, freq=440):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / 8000)))
            for i in range(int(8000 * seconds))))


def main():
    td = tempfile.mkdtemp(prefix="sf-idx-")
    nas = os.path.join(td, "nas")
    os.makedirs(os.path.join(nas, "rock"))
    os.makedirs(os.path.join(nas, "jazz"))
    make_wav(os.path.join(nas, "rock", "song1.wav"), 2.0, 400)
    make_wav(os.path.join(nas, "rock", "song2.wav"), 1.0, 500)
    make_wav(os.path.join(nas, "jazz", "song3.wav"), 1.5, 600)
    with open(os.path.join(nas, "rock", "cover.jpg"), "wb") as f:
        f.write(b"notaudio")
    with open(os.path.join(nas, "jazz", "broken.mp3"), "wb") as f:
        f.write(b"\x00" * 100)  # garbage "mp3"

    db_path = os.path.join(td, "t.db")
    db.migrate(db_path)
    conn = db.connect(db_path)

    # ---- first pass
    stats = scan(conn, nas)
    check("first pass adds all audio", stats["added"] == 4)
    check("non-audio ignored", stats["scanned"] == 4)
    check("scan survives corrupt file", stats["errors"] == 0)
    row = conn.execute("SELECT * FROM tracks WHERE path LIKE '%song1%'"
                       ).fetchone()
    check("duration extracted", abs(row["duration_sec"] - 2.0) < 0.1)
    check("fallback title from filename", row["title"] == "song1")

    # ---- second pass: pure no-op
    stats = scan(conn, nas)
    check("rescan changes nothing",
          stats["added"] == 0 and stats["updated"] == 0)

    # ---- modified file gets re-read
    time.sleep(0.05)
    make_wav(os.path.join(nas, "rock", "song2.wav"), 3.0, 500)
    stats = scan(conn, nas)
    check("modified file updated", stats["updated"] == 1
          and stats["added"] == 0)
    row = conn.execute("SELECT * FROM tracks WHERE path LIKE '%song2%'"
                       ).fetchone()
    check("new duration picked up", abs(row["duration_sec"] - 3.0) < 0.1)

    # ---- new arrival
    make_wav(os.path.join(nas, "jazz", "song4.wav"), 1.0, 700)
    stats = scan(conn, nas)
    check("new file added", stats["added"] == 1)

    # ---- deletion flags missing (row kept — playlists may reference it)
    os.remove(os.path.join(nas, "rock", "song1.wav"))
    stats = scan(conn, nas)
    check("vanished file flagged missing", stats["missing"] == 1)
    row = conn.execute("SELECT missing FROM tracks WHERE path LIKE '%song1%'"
                       ).fetchone()
    check("missing flag set, row kept", row is not None and row["missing"] == 1)

    # ---- file returns -> flag clears
    make_wav(os.path.join(nas, "rock", "song1.wav"), 2.0, 400)
    scan(conn, nas)
    row = conn.execute("SELECT missing FROM tracks WHERE path LIKE '%song1%'"
                       ).fetchone()
    check("returned file un-flagged", row["missing"] == 0)

    # ---- flaky-NAS protection: a whole folder going unreachable must NOT flag
    # its tracks missing (that would wipe them from search). We can't reach into
    # it this pass, so leave those rows exactly as they were.
    import shutil
    shutil.rmtree(os.path.join(nas, "jazz"))
    stats = scan(conn, nas)
    check("unreadable folder doesn't flag its tracks missing",
          stats["missing"] == 0)
    jazz = conn.execute("SELECT missing FROM tracks WHERE path LIKE '%jazz%'"
                        ).fetchall()
    check("that folder's tracks stay searchable (missing=0)",
          bool(jazz) and all(r["missing"] == 0 for r in jazz))
    # but a file removed from a folder that IS still readable is flagged
    os.remove(os.path.join(nas, "rock", "song2.wav"))
    stats = scan(conn, nas)
    check("missing still flags a file in a readable folder",
          stats["missing"] == 1)

    # ---- status row for the GUI tile
    import json
    status = json.loads(db.get_setting(conn, "indexer_status"))
    check("status row written", status["state"] == "idle"
          and "finished_at" in status)

    print(f"INDEXER OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
