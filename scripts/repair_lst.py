"""Repair broken song paths inside ZaraRadio .lst playlists.

When music gets reorganized on the NAS (folders moved/renamed), a .lst keeps
pointing at the old locations and those songs silently get skipped on air.
This finds each entry whose file no longer exists and re-points it to the
same file in its new home, using the music library index (data/studiofire.db,
every file under the NAS music root) — no slow crawl of the share.

Safety rules:
  - EXACT file-name matches only (track number included). A title-only match
    can be a cover or a different version, so it is never used.
  - Several files with that exact name: prefer the one in a folder with the
    same name as before, then the one whose length matches the .lst's stored
    length (within 2s). Still a tie -> left alone and reported.
  - Dry run by default: prints what it WOULD change. --apply writes, after
    backing every file up to data/playlist_backups/<timestamp>/.
  - Keeps the .lst format: count header, '<ms>\\t<path>' lines, CRLF, the
    station's \\\\SERVER\\share path style, cp1252 (UTF-8 + BOM only when a
    path needs characters cp1252 can't hold).

Usage (run from the StudioFire folder):
    python scripts/repair_lst.py "Z:\\John\\*.lst"                 # dry run
    python scripts/repair_lst.py "Z:\\John\\*.lst" --apply
    python scripts/repair_lst.py X.lst --share "\\\\KDPI-Media\\music" --local Z:
"""
import argparse
import collections
import glob
import os
import re
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".aac", ".wav", ".flac", ".ogg"}


def read_lst(path):
    raw = open(path, "rb").read()
    try:
        text, enc = raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        text, enc = raw.decode("cp1252", errors="replace"), "cp1252"
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.isdigit():
            continue                                  # blank / count header
        ms, p = 0, s
        if "\t" in s:
            first, rest = s.split("\t", 1)
            if re.fullmatch(r"-?\d+", first.strip()):
                ms, p = max(0, int(first)), rest.strip()
        is_audio = os.path.splitext(p)[1].lower() in AUDIO_EXTS
        lines.append({"ms": ms, "path": p, "audio": is_audio, "raw": s})
    return lines, enc


def to_local(p, share, local):
    if share and p.lower().startswith(share.lower()):
        return local + p[len(share):]
    return p


def to_share(p, share, local):
    if share and p.lower().startswith(local.lower()):
        return share + p[len(local):]
    return p


def load_index(db_path):
    """basename(lower) -> [(normalized local path, duration_sec)] of files the
    indexer found present."""
    idx = collections.defaultdict(list)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    for path, dur in conn.execute(
            "SELECT path, duration_sec FROM tracks WHERE missing = 0"):
        norm = os.path.normpath(path)                # 'Z:/G\\x' -> 'Z:\\G\\x'
        idx[os.path.basename(norm).lower()].append((norm, dur))
    conn.close()
    return idx


def pick(old_local, want_sec, cands):
    """Best candidate or (None, why)."""
    live = [(p, d) for p, d in cands if os.path.isfile(p)]
    if not live:
        return None, "only in the index, not on disk"
    if len(live) == 1:
        return live[0], "unique"
    old_dir = os.path.basename(os.path.dirname(old_local)).lower()
    same = [c for c in live
            if os.path.basename(os.path.dirname(c[0])).lower() == old_dir]
    if len(same) == 1:
        return same[0], f"{len(live)} copies; same folder name"
    pool = same or live
    if want_sec:
        close = [c for c in pool if c[1] and abs(c[1] - want_sec) <= 2]
        if len(close) == 1:
            return close[0], f"{len(live)} copies; length matches"
        if close:
            pool = close
    # identical files in several places (same size) are fine to take any of
    sizes = {os.path.getsize(p) for p, _ in pool}
    if len(sizes) == 1:
        return sorted(pool)[0], f"{len(live)} identical copies"
    return None, f"{len(live)} different files share this name"


def write_lst(path, lines):
    audio = [ln for ln in lines if ln["audio"]]
    body = [str(len(audio))] + [f"{ln['ms']}\t{ln['path']}" if ln["audio"]
                                 else ln["raw"] for ln in lines]
    text = "\r\n".join(body) + "\r\n"
    try:
        data = text.encode("cp1252")
    except UnicodeEncodeError:
        data = text.encode("utf-8-sig")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("files", nargs="+", help=".lst files or globs")
    ap.add_argument("--apply", action="store_true",
                    help="write the fixes (after backing up)")
    ap.add_argument("--share", default="\\\\KDPI-Media\\music",
                    help="station UNC prefix used inside the .lst files")
    ap.add_argument("--local", default="Z:",
                    help="where that share is reachable on this PC")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "studiofire.db"))
    ap.add_argument("--also", action="append", default=[], metavar="DIR",
                    help="extra folder to search (recursively) for files the "
                         "index doesn't cover, e.g. ID/PSA folders outside "
                         "the music root. Repeatable.")
    args = ap.parse_args()

    files = []
    for f in args.files:
        files += sorted(glob.glob(f)) or [f]
    print("loading library index…", flush=True)
    idx = load_index(args.db)
    print(f"  {sum(len(v) for v in idx.values()):,} files indexed")
    for extra in args.also:
        n = 0
        for dirpath, _dirs, names in os.walk(extra):
            for name in names:
                if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                    full = os.path.normpath(os.path.join(dirpath, name))
                    if all(full.lower() != p.lower()
                           for p, _ in idx[name.lower()]):
                        idx[name.lower()].append((full, None))
                        n += 1
        print(f"  + {n:,} files from {extra}")
    print()
    bdir = None
    totals = collections.Counter()
    for path in files:
        try:
            lines, enc = read_lst(path)
        except OSError as exc:
            print(f"!! {path}: {exc}")
            continue
        audio = [ln for ln in lines if ln["audio"]]
        fixed, unresolved, ok = [], [], 0
        for ln in audio:
            local = to_local(ln["path"], args.share, args.local)
            if os.path.isfile(local):
                ok += 1
                continue
            cands = idx.get(os.path.basename(local).lower(), [])
            if not cands:
                unresolved.append((ln["path"], "not found in the library"))
                continue
            best, why = pick(local, ln["ms"] / 1000.0, cands)
            if best is None:
                unresolved.append((ln["path"], why))
                continue
            new = to_share(best[0], args.share, args.local)
            fixed.append((ln["path"], new, why))
            ln["path"] = new
            if not ln["ms"] and best[1]:
                ln["ms"] = int(round(best[1] * 1000))
        totals.update(ok=ok, fixed=len(fixed), unresolved=len(unresolved))
        print(f"== {os.path.basename(path)}: {len(audio)} songs — {ok} OK, "
              f"{len(fixed)} re-pointed, {len(unresolved)} not found")
        for old, new, why in fixed:
            print(f"   FIX  {old}\n     -> {new}   [{why}]")
        for old, why in unresolved:
            print(f"   ??   {old}   [{why}]")
        if args.apply and fixed:
            if bdir is None:
                bdir = os.path.join(ROOT, "data", "playlist_backups",
                                    time.strftime("%Y%m%d-%H%M%S"))
                os.makedirs(bdir)
            shutil.copy2(path, os.path.join(bdir, os.path.basename(path)))
            write_lst(path, lines)
            print(f"   written (backup in {bdir})")
        print()
    print(f"TOTAL: {totals['ok']} OK, {totals['fixed']} re-pointed, "
          f"{totals['unresolved']} not found"
          + ("" if args.apply else "   (dry run — nothing written; add "
             "--apply)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
