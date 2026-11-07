"""Backfill play attribution for airings that happened before migration 15.

Before migration 15, `play_history.extra` was empty on every row, so no report
could say which playlist (or spot rule) a track belonged to. The information was
not lost, though — two things in the existing data reconstruct it:

* a `track_end` row carries the ORIGINAL library path in its extra (`src`), and it
  pairs with the `track_start` row for the same precache file;
* `playlist_items.path` / the spot-rule folders are keyed by that same library
  path.

So: pair start<->end by path, take the library path, look up the playlist item or
the spot rule, and write it into `extra`. Rows that cannot be paired or matched
are left alone — a backfill must never invent an attribution.

Usage:
    python scripts/backfill_play_attribution.py <db path> [--dry-run] [--limit N]

Safe to re-run: it only fills rows whose extra has no attribution yet.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db as coredb        # noqa: E402
from services.core import spots as spotmod    # noqa: E402


def _norm(p: str | None) -> str:
    return str(p or "").strip().replace("/", "\\").casefold()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    limit = None
    for a in sys.argv[1:]:
        if a.startswith("--limit"):
            limit = int(a.split("=", 1)[1]) if "=" in a else None
    if not args:
        print(__doc__)
        return 2
    db_path = args[0]
    conn = coredb.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ---- library path by precache path, from the paired track_end rows ------
    ends: dict[str, list[tuple[str, str]]] = {}
    for r in conn.execute(
            "SELECT ts, path, extra FROM play_history "
            "WHERE event='track_end' AND path IS NOT NULL").fetchall():
        try:
            src = (json.loads(r["extra"] or "{}") or {}).get("src")
        except ValueError:
            src = None
        if src:
            ends.setdefault(r["path"], []).append((r["ts"], src))

    # ---- playlist item lookup by library path ------------------------------
    item_by_path: dict[str, tuple[int, int]] = {}
    for r in conn.execute("SELECT id, playlist_id, path FROM playlist_items"
                          ).fetchall():
        item_by_path.setdefault(_norm(r["path"]), (r["playlist_id"], r["id"]))

    # ---- spot folders -> rule ---------------------------------------------
    rules = [dict(r) for r in conn.execute("SELECT * FROM spot_rules").fetchall()]
    rule_folders = []
    for rule in rules:
        for key in ("folder_path", "file_path"):
            v = _norm(rule.get(key))
            if v:
                rule_folders.append((v, rule["id"]))

    def match_spot(src: str) -> int | None:
        s = _norm(src)
        best = None
        for folder, rid in rule_folders:
            if s == folder or s.startswith(folder + "\\"):
                if best is None or len(folder) > best[0]:
                    best = (len(folder), rid)
        return best[1] if best else None

    rows = conn.execute(
        "SELECT id, ts, path, extra FROM play_history "
        "WHERE event='track_start' AND path IS NOT NULL "
        "AND (extra IS NULL OR json_extract(extra, '$.attr_kind') IS NULL) "
        "ORDER BY id").fetchall()
    if limit:
        rows = rows[:limit]

    filled = unmatched = 0
    for r in rows:
        pairs = ends.get(r["path"]) or []
        src = None
        for ts, sp in pairs:
            if ts and ts >= (r["ts"] or ""):
                src = sp
                break
        if src is None and pairs:
            src = pairs[0][1]
        try:
            extra = json.loads(r["extra"] or "{}") or {}
        except ValueError:
            extra = {}
        if not src:
            unmatched += 1
            continue
        hit = item_by_path.get(_norm(src))
        if hit:
            extra.update({"attr_kind": "playlist", "playlist_id": hit[0],
                          "item_id": hit[1], "backfilled": True})
            filled += 1
        else:
            rid = match_spot(src)
            if rid:
                extra.update({"attr_kind": "spot", "spot_rule_id": rid,
                              "backfilled": True})
                filled += 1
            else:
                unmatched += 1
                continue
        if not dry:
            conn.execute("UPDATE play_history SET extra = ? WHERE id = ?",
                         (json.dumps(extra), r["id"]))
    if not dry:
        conn.commit()
    print("rows considered : %d" % len(rows))
    print("attributed      : %d" % filled)
    print("could not match : %d" % unmatched)
    print("mode            : %s" % ("DRY RUN (nothing written)" if dry else "written"))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
