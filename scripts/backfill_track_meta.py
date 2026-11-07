"""Fill genre / year / label for tracks that were indexed before migration 16.

The library files have carried these tags all along; the indexer never read them.
This walks the existing index (never a directory listing — an os.walk over the
share times out), re-opens each file ONCE, stores the extra tags, and marks
`meta_read = 1` so a file with no genre is not re-opened on every run.

Read-mostly, additive, resumable: run it as often as you like, it only touches
rows where meta_read = 0.

Usage:
    python scripts/backfill_track_meta.py <db path> [--limit N] [--dry-run]
                                          [--pause 0.05]
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db as coredb                 # noqa: E402
from services.worker.indexer import read_tags          # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    db_path = args[0]
    limit = None
    pause = 0.02
    for a in sys.argv[1:]:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
        if a.startswith("--pause="):
            pause = float(a.split("=", 1)[1])
    dry = "--dry-run" in sys.argv
    played_only = "--played" in sys.argv

    conn = coredb.connect(db_path)
    conn.row_factory = sqlite3.Row
    if played_only:
        # the songs that actually aired — enough to make the genre/artist
        # reports real without waiting for a 283k-file sweep
        sql = ("SELECT DISTINCT pi.path AS path FROM playlist_items pi "
               "JOIN play_history ph "
               "  ON json_extract(ph.extra, '$.item_id') = pi.id "
               "WHERE pi.path IS NOT NULL AND pi.path <> ''")
    else:
        sql = ("SELECT path FROM tracks WHERE meta_read = 0 AND missing = 0 "
               "ORDER BY id")
    if limit:
        sql += " LIMIT %d" % limit
    paths = [r["path"] for r in conn.execute(sql).fetchall()]
    print("track rows to enrich: %d%s" % (len(paths), " (dry run)" if dry else ""))

    done = skipped = failed = with_genre = 0
    t0 = time.time()
    for i, path in enumerate(paths, 1):
        if not os.path.exists(path):
            skipped += 1
            if not dry:
                with conn:
                    conn.execute("UPDATE tracks SET missing = 1, meta_read = 1 "
                                 "WHERE path = ?", (path,))
            continue
        tags = read_tags(path)
        if tags.get("genre"):
            with_genre += 1
        if not dry:
            # Update EVERY row that points at this file. The index can hold the
            # same song twice with different separators (forward vs back
            # slashes); enriching only the exact-path row leaves the other one
            # blank, and the genre report joins the blank one -> "Unknown".
            with conn:
                # artist/title/album too, but only where they are missing:
                # rows indexed before the tag phase finished still have a
                # filename as the title, and the artist report needs the tag.
                conn.execute(
                    "UPDATE tracks SET genre = ?, year = ?, label = ?, "
                    "  artist = COALESCE(artist, ?), "
                    "  album  = COALESCE(album, ?), "
                    "  title  = COALESCE(NULLIF(title, ''), ?), "
                    "  meta_read = 1 "
                    "WHERE lower(replace(path, '/', char(92))) = "
                    "      lower(replace(?, '/', char(92)))",
                    (tags.get("genre"), tags.get("year"), tags.get("label"),
                     tags.get("artist"), tags.get("album"), tags.get("title"),
                     path))
        done += 1
        if pause:
            time.sleep(pause)
        if i % 200 == 0:
            rate = done / max(0.001, time.time() - t0)
            left = len(paths) - i
            print("  %d/%d  (%.1f/s, ~%.0f min left)  genre found in %d"
                  % (i, len(paths), rate, left / max(0.001, rate) / 60,
                     with_genre), flush=True)

    print("\nenriched   : %d" % done)
    print("with genre : %d (%.0f%%)" % (
        with_genre, 100.0 * with_genre / done if done else 0))
    print("missing    : %d" % skipped)
    print("failed     : %d" % failed)
    print("mode       : %s" % ("DRY RUN (nothing written)" if dry else "written"))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
