"""How much music-metadata is actually IN the files, offline?

Samples N library files and reports which tags exist (genre / year / label /
ISRC / country / BPM) — the evidence needed before promising an enrichment DB.
Read-only. Usage: python sample_tags.py [N] [root]
"""
import os
import sys

sys.path.insert(0, r"C:\Users\markd\Desktop\Projects\StudioFire")
from mutagen import File as MFile  # noqa: E402

TAGS = {
    "genre": ("genre", "TCON", "\xa9gen"),
    "year": ("date", "year", "TYER", "TDRC", "\xa9day"),
    "label": ("label", "publisher", "organization", "TPUB", "\xa9pub"),
    "isrc": ("isrc", "TSRC"),
    "country": ("country", "TCON"),
    "bpm": ("bpm", "TBPM"),
}
EXTS = (".mp3", ".flac", ".m4a", ".wav")


def scan(paths):
    stats = {k: 0 for k in TAGS}
    seen = 0
    samples = {}
    for p in paths:
        try:
            f = MFile(p, easy=False)
        except Exception:
            continue
        if f is None:
            continue
        seen += 1
        for key, names in TAGS.items():
            found = None
            for nm in names:
                try:
                    v = f.get(nm)
                except Exception:
                    v = None
                if v:
                    found = str(v)[:60]
                    break
            if found:
                stats[key] += 1
                samples.setdefault(key, (os.path.basename(p), found))
    return seen, stats, samples


def walk(root, limit):
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith(EXTS):
                out.append(os.path.join(dirpath, fn))
                if len(out) >= limit:
                    return out
    return out


def from_db(db_path, n, offset=0):
    """Pick sample files from the index instead of walking the share: an
    a directory walk over the NAS share times out long before it finds 40 files."""
    import sqlite3
    conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    rows = conn.execute(
        "SELECT path FROM tracks WHERE missing = 0 AND path LIKE '%.mp3' "
        "ORDER BY id LIMIT ? OFFSET ?", (n, offset)).fetchall()
    conn.close()
    return [r[0].replace("/", "\\") for r in rows]


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    root = sys.argv[2] if len(sys.argv) > 2 else r"Z:\G"
    if root.lower().endswith(".db"):
        files = from_db(root, n)
    else:
        files = walk(root, n)
    seen, stats, samples = scan(files)
    print("files sampled: %d (found %d audio files)" % (seen, len(files)))
    for k, v in stats.items():
        pct = (100.0 * v / seen) if seen else 0
        print("  %-8s %5.1f%%  e.g. %s" % (k, pct, samples.get(k, "-")))
