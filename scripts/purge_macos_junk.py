"""Remove macOS resource-fork rows ("._name.mp3", ".DS_Store") from the library.

Those files carry an audio extension but contain no audio. The indexer now skips
them; this clears the rows an older scan already stored. They are not real
tracks, so nothing should reference them — the script checks before deleting and
refuses if anything does.

    python scripts/purge_macos_junk.py            # dry run (default)
    python scripts/purge_macos_junk.py --apply    # delete
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import paths as shared_paths          # noqa: E402
from services.core import config as core_config    # noqa: E402

JUNK = ("path LIKE '%/._%' OR path LIKE '%\\._%' OR path LIKE '%/.DS_Store' "
        "OR path LIKE '%\\.DS_Store'")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="database (default: config)")
    ap.add_argument("--apply", action="store_true", help="actually delete")
    args = ap.parse_args()

    db = args.db
    if not db:
        cfg = core_config.load_config(
            os.path.join(core_config.ROOT, "config", "config.json"))
        db = os.path.join(shared_paths.resolve(cfg, core_config.ROOT)["data_dir"],
                          "studiofire.db")
    if not os.path.isfile(db):
        print("no database at", db)
        return 2

    conn = sqlite3.connect(db)
    try:
        total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        junk = conn.execute(
            f"SELECT COUNT(*) FROM tracks WHERE {JUNK}").fetchone()[0]
        print(f"library rows : {total:,}")
        print(f"macOS junk   : {junk:,}")
        if not junk:
            return 0

        # refuse if anything points at them (play history, playlist items)
        refs = 0
        for table, col in (("play_history", "path"), ("playlist_items", "path")):
            try:
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col} IN "
                    f"(SELECT path FROM tracks WHERE {JUNK})").fetchone()[0]
            except sqlite3.Error:
                continue
            if n:
                print(f"  {table}: {n:,} rows reference junk paths — "
                      f"not deleting")
                refs += n
        if refs:
            print("Nothing was deleted. Say the word and I will clear those "
                  "references first.")
            return 3

        sample = [r[0] for r in conn.execute(
            f"SELECT path FROM tracks WHERE {JUNK} LIMIT 3")]
        for s in sample:
            print("  e.g.", s)
        if not args.apply:
            print("\ndry run — re-run with --apply to delete")
            return 0
        with conn:
            conn.execute(f"DELETE FROM tracks WHERE {JUNK}")
        left = conn.execute(
            f"SELECT COUNT(*) FROM tracks WHERE {JUNK}").fetchone()[0]
        print(f"deleted {junk:,} rows; {left} junk rows remain")
        return 0 if left == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
