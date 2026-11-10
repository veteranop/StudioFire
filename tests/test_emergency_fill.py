"""Wizard promise: "keep a local emergency copy filled from my library".

The emergency step saved `emergency_fill_from_library` and NOTHING read it, so
the folder stayed empty and the failover chain fell past it to the precache and
then (on a fresh install) to the baked-in tone — the constant-tone report in
ticket 1003. These checks pin the fill itself: what it copies, what it refuses
to copy, and that it never damages a curated folder.

Run: python tests/test_emergency_fill.py
"""
import math
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import setup as setup_mod                     # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def make_wav(path, seconds=1.0, freq=440):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        frames = b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / 8000)))
            for i in range(int(8000 * seconds)))
        w.writeframes(frames)


def make_db(path, track_paths):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tracks (path TEXT, missing INTEGER DEFAULT 0)")
    for p, missing in track_paths:
        conn.execute("INSERT INTO tracks (path, missing) VALUES (?, ?)",
                     (p, missing))
    conn.commit()
    return conn


def audio_files(folder):
    return sorted(f for f in os.listdir(folder)
                  if f.lower().endswith((".mp3", ".wav", ".flac", ".m4a")))


def main():
    td = tempfile.mkdtemp(prefix="sf-emfill-")
    try:
        lib = os.path.join(td, "library")
        good = []
        for i, name in enumerate(("one.mp3", "two.flac")):
            p = os.path.join(lib, name)
            make_wav(p, freq=300 + 100 * i)
            good.append(p)
        gone = os.path.join(lib, "gone.mp3")          # in the index, not on disk
        notes = os.path.join(lib, "cover.jpg")        # not audio
        make_wav(notes)

        conn = make_db(os.path.join(td, "core.db"), [
            (good[0], 0), (good[1], 0), (gone, 0), (notes, 0),
            (good[0], 1),                              # flagged missing
        ])
        em = os.path.join(td, "emergency")
        cfg = {"paths": {"emergency_dir": em, "data_root": td}}

        made = setup_mod.fill_emergency_from_library(conn, cfg, count=5)
        check("copied the two real audio tracks", made == 2)
        check("both files are in the emergency folder",
              audio_files(em) == ["one.mp3", "two.flac"])
        check("no .part files left behind",
              not any(f.endswith(".part") for f in os.listdir(em)))
        check("copies are byte-identical",
              open(good[0], "rb").read()
              == open(os.path.join(em, "one.mp3"), "rb").read())
        check("a missing file and a non-audio file were skipped",
              "gone.mp3" not in audio_files(em)
              and "cover.jpg" not in os.listdir(em))

        # never touches a folder that already has audio
        made_again = setup_mod.fill_emergency_from_library(conn, cfg, count=5)
        check("an already-filled folder is left alone", made_again == 0)
        check("still exactly the two files", len(audio_files(em)) == 2)

        # budget + count are honoured
        em2 = os.path.join(td, "emergency2")
        made_one = setup_mod.fill_emergency_from_library(
            conn, {"paths": {"emergency_dir": em2, "data_root": td}}, count=1)
        check("count cap respected", made_one == 1)

        em3 = os.path.join(td, "emergency3")
        made_none = setup_mod.fill_emergency_from_library(
            conn, {"paths": {"emergency_dir": em3, "data_root": td}},
            max_bytes=1)
        check("byte budget refuses anything too big", made_none == 0)

        # a curated folder (operator's own ID files) is never re-filled
        em4 = os.path.join(td, "emergency4")
        make_wav(os.path.join(em4, "station-id.wav"), freq=600)
        made_curated = setup_mod.fill_emergency_from_library(
            conn, {"paths": {"emergency_dir": em4, "data_root": td}})
        check("curated folder untouched", made_curated == 0
              and audio_files(em4) == ["station-id.wav"])

        conn.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print(f"\n{passed} checks passed")


if __name__ == "__main__":
    main()
