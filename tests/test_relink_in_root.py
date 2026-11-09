"""relink_broken must repair a file that moved WITHIN the music root, and land
on the file's REAL new home, not merely "some file with the same name".

Origin (2026-11-08 Drop-in Radio, "a lot of songs are reporting missing, but
they're there"): the NAS library was reorganised under the playlists — folders
un-nested (``G\\New JB Music\\<Artist>`` -> ``G\\<Artist>``), renamed and
flattened, and whole new folders created. Every stale path was still UNDER
nas_music_root, so the old relink_broken (which only looked OUTSIDE the root)
counted them "in_place" and re-pointed nothing: the operator's "Fix broken file
paths" button said 0 fixed while 66% of the on-air playlist was dead.

This suite covers the matching ladder that replaced the bare-basename guess:
  * a folder un-nested / re-encoded in place      -> trailing-segment match
  * a folder renamed AND flattened (name only)    -> bare-basename fallback
  * a file moved into a brand-new folder          -> bare-basename fallback
  * a generic name whose only survivor is a
    different recording (size+duration disagree)  -> REFUSED, left dead
  * same name in 2+ folders                        -> ambiguous, left alone
  * the quarantine, own-path, unknown-under-root,
    alive and idempotency cases
  * on-disk verification + self-heal (restore)    -> needs a real folder

Run: python tests/test_relink_in_root.py
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db, playlists as pl    # noqa: E402

passed = 0
MUSIC = r"\\NAS\music"


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def add_track(conn, path, missing=0, size=None, duration=None):
    conn.execute(
        "INSERT INTO tracks (path, title, duration_sec, size, indexed_at, "
        "missing) VALUES (?, ?, ?, ?, ?, ?)",
        (path, os.path.basename(path), duration, size, 1.0, missing))
    conn.commit()


def item_path(conn, iid):
    return conn.execute("SELECT path FROM playlist_items WHERE id = ?",
                        (iid,)).fetchone()["path"]


def missing_flag(conn, path):
    return conn.execute("SELECT missing FROM tracks WHERE path = ?",
                        (path,)).fetchone()["missing"]


# ---------------------------------------------------------------- matching ladder
def test_matching():
    td = tempfile.mkdtemp(prefix="sf-relink-")
    db_path = os.path.join(td, "t.db")
    db.migrate(db_path)
    conn = db.connect(db_path)

    # (1) folder un-nested: name + 2 folders survive -> trailing-segment match.
    # A same-named decoy in an unrelated folder proves depth disambiguates.
    moved_old = MUSIC + r"\G\New JB Music\Neil Young\Harvest\x.mp3"
    moved_new = MUSIC + r"\G\Neil Young\Harvest\x.mp3"
    add_track(conn, moved_old, missing=1, size=10, duration=200.0)
    add_track(conn, moved_new, missing=0, size=10, duration=200.0)
    add_track(conn, MUSIC + r"\G\Wrong\Random\x.mp3", missing=0)   # depth-1 decoy

    # (2) folder renamed AND flattened: only the file name survives. Same file
    # (size+duration agree) -> bare-basename fallback re-points it.
    flat_old = MUSIC + r"\G\Charlie Parker & Miles - Birdsong\18 Big Foot.mp3"
    flat_new = MUSIC + r"\G\charlie parker new\18 Big Foot.mp3"
    add_track(conn, flat_old, missing=1, size=100, duration=50.0)
    add_track(conn, flat_new, missing=0, size=100, duration=50.0)

    # (3) moved into a BRAND-NEW folder, nothing tagged yet (size/duration
    # unknown on both sides) -> still resolves on the name alone.
    newf_old = MUSIC + r"\G\Old Singles\solo.mp3"
    newf_new = MUSIC + r"\G\aaa singles 3\solo.mp3"
    add_track(conn, newf_old, missing=1)
    add_track(conn, newf_new, missing=0)

    # (4) generic name, the only survivor is a DIFFERENT recording (a parody
    # cover): size AND duration disagree -> REFUSED, never put on air.
    conf_old = MUSIC + r"\G\Led Zeppelin\IV\black dog.mp3"
    conf_new = MUSIC + r"\G\Dread Zeppelin\Un-Led-Ed\black dog.mp3"
    add_track(conn, conf_old, missing=1, size=5000, duration=300.0)
    add_track(conn, conf_new, missing=0, size=4000, duration=200.0)

    # (5) truly gone: quarantined folder since emptied, no survivor at all.
    gone_old = MUSIC + r"\G\_Duplicates\2026-10-06\Nowhere\y.mp3"
    add_track(conn, gone_old, missing=1)

    # (6) healthy item — index says it is present.
    alive = MUSIC + r"\G\Adele\01 Daydreamer.mp3"
    add_track(conn, alive, missing=0)

    # (7) same name in two folders, nothing to break the tie -> ambiguous.
    amb_old = MUSIC + r"\G\Old\dup.mp3"
    add_track(conn, amb_old, missing=1)
    add_track(conn, MUSIC + r"\G\A\dup.mp3", missing=0)
    add_track(conn, MUSIC + r"\G\B\dup.mp3", missing=0)

    # (8) pre-NAS import: no index row at all, lives outside the root -> old
    # behaviour, re-point at the unique same-named file.
    outside_old = r"Z:\Old Imports\song.mp3"
    outside_new = MUSIC + r"\G\Imported\song.mp3"
    add_track(conn, outside_new, missing=0)

    # (9) a quarantined copy is NOT a repair target: that folder gets emptied.
    quar_old = MUSIC + r"\G\OldQ\quar.mp3"
    add_track(conn, quar_old, missing=1)
    add_track(conn, MUSIC + r"\G\_Duplicates\2026-11-08\OldQ\quar.mp3", missing=0)

    pid = pl.create_playlist(conn, "On Air")
    i_moved = pl.add_item(conn, pid, "file", moved_old, "x")
    i_flat = pl.add_item(conn, pid, "file", flat_old, "bf")
    i_newf = pl.add_item(conn, pid, "file", newf_old, "solo")
    i_conf = pl.add_item(conn, pid, "file", conf_old, "bd")
    i_gone = pl.add_item(conn, pid, "file", gone_old, "y")
    i_alive = pl.add_item(conn, pid, "file", alive, "z")
    i_amb = pl.add_item(conn, pid, "file", amb_old, "dup")
    i_out = pl.add_item(conn, pid, "file", outside_old, "s")
    i_unknown = pl.add_item(conn, pid, "file",
                            MUSIC + r"\G\Not\Scanned.mp3", "n")
    i_quar = pl.add_item(conn, pid, "file", quar_old, "quar")
    i_folder = pl.add_item(conn, pid, "folder-rotation", MUSIC + r"\G\A", "A")

    stats = pl.relink_broken(conn, MUSIC, pid)

    # ---- ladder stage 1: trailing-segment match beats a shallow same-name decoy
    check("un-nested folder re-pointed by trailing segments",
          item_path(conn, i_moved) == moved_new)
    check("...counted under by_suffix", stats["by_suffix"] == 1)

    # ---- ladder stage 3: renamed+flattened and brand-new folder, name only
    check("renamed+flattened folder re-pointed by name",
          item_path(conn, i_flat) == flat_new)
    check("file moved into a brand-new folder re-pointed by name",
          item_path(conn, i_newf) == newf_new)
    check("...both counted under by_basename", stats["by_basename"] == 3)

    # ---- the accuracy guard: a different recording with the same name is refused
    check("generic name whose only survivor is a different recording is REFUSED",
          item_path(conn, i_conf) == conf_old)
    check("...counted as a conflict, not a re-point", stats["conflict"] == 1)

    # ---- totals
    check("relinked = suffix + basename + size", stats["relinked"] == 4)
    check("recognised the dead ones", stats["dead"] == 7)
    check("verified_on_disk is False when the share is unreachable",
          stats["verified_on_disk"] is False)

    # ---- truly-gone / ambiguous / quarantine are never guessed at
    check("no-survivor item left alone", item_path(conn, i_gone) == gone_old)
    check("two folders => ambiguous, untouched", item_path(conn, i_amb) == amb_old)
    check("...counted ambiguous", stats["ambiguous"] == 1)
    check("never re-pointed into the quarantine folder",
          item_path(conn, i_quar) == quar_old)
    check("gone + quarantine counted unmatched", stats["unmatched"] == 2)
    check("...and a basename is named for the operator",
          "y.mp3" in stats["unmatched_examples"])

    # ---- things that were already right are left alone
    check("healthy item untouched", item_path(conn, i_alive) == alive)
    check("not-indexed item under the root untouched (not indexed != dead)",
          item_path(conn, i_unknown) == MUSIC + r"\G\Not\Scanned.mp3")
    check("pre-NAS outside-root import still re-pointed",
          item_path(conn, i_out) == outside_new)
    check("folder item untouched", item_path(conn, i_folder) == MUSIC + r"\G\A")
    check("checked counts file items only", stats["checked"] == 10)

    # ---- idempotent: a second pass changes nothing and finds nothing to do
    s2 = pl.relink_broken(conn, MUSIC, pid)
    check("second pass re-points nothing", s2["relinked"] == 0)
    check("second pass leaves only the truly-stuck ones dead", s2["dead"] == 4)
    check("second pass still refuses the conflict", s2["conflict"] == 1)
    conn.close()
    shutil.rmtree(td, ignore_errors=True)


# ----------------------------------------- extension / copy-marker matching
def test_stem_matching():
    """The same recording under a different file extension, or with a copy marker
    from the duplicate cleaner, must still be found.

    The live library has been through several transcodes (mp3 -> m4a / wav) and the
    duplicate cleaner appends " (1)", so a name-with-extension comparison misses
    songs that are demonstrably still there — exactly the client's "when I go to
    replace these, the files are in the library".
    """
    td = tempfile.mkdtemp(prefix="sf-relink-stem-")
    db_path = os.path.join(td, "t.db")
    db.migrate(db_path)
    conn = db.connect(db_path)

    # (a) same album folder, .mp3 gone, .m4a present -> trailing-segment match
    dead_ext = MUSIC + r"\G\Roxy Music\The Best Of\02 More Than This.mp3"
    live_ext = MUSIC + r"\G\Roxy Music\The Best Of\02 More Than This.m4a"
    add_track(conn, dead_ext, missing=1, size=1000, duration=258.5)
    add_track(conn, live_ext, missing=0, size=2400, duration=258.5)

    # (b) duplicate-cleaner copy marker in the same folder
    dead_cp = MUSIC + r"\G\Beta Band\Heroes\05 Wonderful (1).mp3"
    live_cp = MUSIC + r"\G\Beta Band\Heroes\05 Wonderful.mp3"
    add_track(conn, dead_cp, missing=1, size=1000, duration=200.0)
    add_track(conn, live_cp, missing=0, size=1000, duration=200.0)

    # (c) same name, different album folder, and the index proves a different
    # recording (size AND duration disagree) -> REFUSED, not re-pointed
    dead_wrong = MUSIC + r"\G\Van Morrison\Astral Weeks\03 Sweet Thing.mp3"
    add_track(conn, dead_wrong, missing=1, size=9000000, duration=265.7)
    add_track(conn, MUSIC + r"\G\David Bowie\Diamond Dogs\03 Sweet Thing.m4a",
              missing=0, size=4000000, duration=218.8)

    # (d) .wav twin in a NEW folder: size differs (transcode) but the length agrees
    dead_wav = MUSIC + r"\John\Music\JB\The Tontons - Golden.mp3"
    live_wav = MUSIC + r"\G\Studio Fire Playlist #2\The Tontons - Golden.wav"
    add_track(conn, dead_wav, missing=1, size=1000, duration=200.0)
    add_track(conn, live_wav, missing=0, size=4000, duration=200.2)

    pid = pl.create_playlist(conn, "Stems")
    i_ext = pl.add_item(conn, pid, "file", dead_ext, "ext")
    i_cp = pl.add_item(conn, pid, "file", dead_cp, "cp")
    i_wrong = pl.add_item(conn, pid, "file", dead_wrong, "wrong")
    i_wav = pl.add_item(conn, pid, "file", dead_wav, "wav")

    stats = pl.relink_broken(conn, MUSIC, pid)

    check("mp3 item found via its .m4a twin in the same album", item_path(conn, i_ext) == live_ext)
    check("copy-marked item found via the un-marked file", item_path(conn, i_cp) == live_cp)
    check(".wav transcode in a new folder is accepted when the length agrees",
          item_path(conn, i_wav) == live_wav)
    check("a different recording with the same name is still REFUSED",
          item_path(conn, i_wrong) == dead_wrong)
    check("...counted as a conflict", stats["conflict"] == 1)
    check("three of the four were re-pointed", stats["relinked"] == 3)
    conn.close()
    shutil.rmtree(td, ignore_errors=True)


# --------------------------------------------- on-disk verification + self-heal
def test_disk_verification():
    """When the share IS reachable, the target is confirmed on disk before the
    rewrite, and a restored item heals in place instead of being hijacked."""
    td = tempfile.mkdtemp(prefix="sf-relink-disk-")
    root = os.path.join(td, "music")

    def touch(rel):
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(b"x")
        return full

    db_path = os.path.join(td, "t.db")
    db.migrate(db_path)
    conn = db.connect(db_path)

    # (a) RESTORE: the item's own file is back on disk; the index still flags it
    # missing and a same-named copy exists elsewhere. It must NOT be hijacked —
    # heal it in place and clear the index flag.
    restored = touch(r"Artist\restored.mp3")           # own file present again
    add_track(conn, restored, missing=1)
    hijack = touch(r"Other\restored.mp3")              # a tempting same-named copy
    add_track(conn, hijack, missing=0)

    # (b) STALE INDEX: candidate is present in the index but gone from disk.
    # The verify step must refuse it rather than swap one dead entry for another.
    stale_old = os.path.join(root, r"Gone\song2.mp3")  # own file absent
    add_track(conn, stale_old, missing=1)
    stale_cand = os.path.join(root, r"Stale\song2.mp3")  # indexed but not on disk
    add_track(conn, stale_cand, missing=0)

    # (c) GOOD: candidate present in the index AND on disk -> re-point.
    good_old = os.path.join(root, r"GoneB\song3.mp3")  # own file absent
    add_track(conn, good_old, missing=1)
    good_new = touch(r"NewB\song3.mp3")
    add_track(conn, good_new, missing=0)

    pid = pl.create_playlist(conn, "Disk")
    i_restore = pl.add_item(conn, pid, "file", restored, "r")
    i_stale = pl.add_item(conn, pid, "file", stale_old, "s2")
    i_good = pl.add_item(conn, pid, "file", good_old, "s3")

    stats = pl.relink_broken(conn, root, pid)

    check("verified_on_disk is True when the share is reachable",
          stats["verified_on_disk"] is True)
    check("restored item is NOT hijacked", item_path(conn, i_restore) == restored)
    check("...its index flag is cleared (self-heal)",
          missing_flag(conn, restored) == 0)
    check("...counted self_healed", stats["self_healed"] == 1)
    check("stale index candidate is refused", item_path(conn, i_stale) == stale_old)
    check("...counted unverified", stats["unverified"] == 1)
    check("verified candidate IS re-pointed", item_path(conn, i_good) == good_new)
    check("...counted relinked", stats["relinked"] == 1)
    conn.close()
    shutil.rmtree(td, ignore_errors=True)


def main():
    test_matching()
    test_stem_matching()
    test_disk_verification()
    print("\n%d checks passed" % passed)


if __name__ == "__main__":
    main()
