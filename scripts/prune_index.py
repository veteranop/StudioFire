"""Flag library-index tracks whose FOLDER is gone (moved/deleted), fast.

After a big reorganization of the NAS (artist folders re-filed elsewhere),
the index can keep listing thousands of songs as present at their old
locations until the next full rescan (hours over a VPN). This checks every
folder the index references — in parallel — and flags the tracks of any
folder that is PROVEN gone: its nearest existing parent folder can be read
and does not list it. A folder that exists but can't be read proves nothing
and is left alone (same rule as the indexer). Rows are only flagged
missing=1, never deleted; the next indexer pass un-flags anything that
turns out to be there.

    python scripts/prune_index.py            # dry run: report only
    python scripts/prune_index.py --apply
"""
import argparse
import collections
import concurrent.futures
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "studiofire.db"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    by_dir = collections.defaultdict(list)
    for (path,) in conn.execute("SELECT path FROM tracks WHERE missing = 0"):
        by_dir[os.path.normpath(os.path.dirname(path))].append(path)
    dirs = sorted(by_dir)
    print(f"{sum(len(v) for v in by_dir.values()):,} present tracks in "
          f"{len(dirs):,} folders — checking folders…", flush=True)

    with concurrent.futures.ThreadPoolExecutor(32) as pool:
        exists = dict(zip(dirs, pool.map(os.path.isdir, dirs)))
    missing_dirs = [d for d in dirs if not exists[d]]
    print(f"{len(missing_dirs):,} folders not found — proving each is gone…",
          flush=True)

    listing_cache: dict = {}

    def listing(d):
        if d not in listing_cache:
            try:
                listing_cache[d] = {n.lower() for n in os.listdir(d)}
            except OSError:
                listing_cache[d] = None          # unreadable: proves nothing
        return listing_cache[d]

    def proven_gone(d):
        child, parent = d, os.path.dirname(d)
        while parent and parent != child:
            if exists.get(parent) is False:      # parent itself gone: climb
                child, parent = parent, os.path.dirname(parent)
                continue
            names = listing(parent)
            if names is None:
                if parent not in exists:
                    exists[parent] = os.path.isdir(parent)
                if exists[parent] is False:      # also gone: keep climbing
                    child, parent = parent, os.path.dirname(parent)
                    continue
                return False                     # exists, unreadable: flaky
            return os.path.basename(child).lower() not in names
        return False

    gone_dirs = [d for d in missing_dirs if proven_gone(d)]
    tracks = [p for d in gone_dirs for p in by_dir[d]]
    print(f"proven gone: {len(gone_dirs):,} folders, {len(tracks):,} tracks "
          f"({len(missing_dirs) - len(gone_dirs)} folders unprovable — "
          "left alone)")
    for d in gone_dirs[:10]:
        print("   ", d)
    if args.apply and tracks:
        with conn:
            conn.executemany("UPDATE tracks SET missing = 1 WHERE path = ?",
                             [(p,) for p in tracks])
        print(f"flagged {len(tracks):,} tracks missing")
    elif not args.apply:
        print("(dry run — add --apply)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
