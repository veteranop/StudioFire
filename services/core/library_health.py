"""Library health — what each playlist will actually play, and what is gone.

Read-only views over the LOCAL index. Nothing here walks the music share: the
indexer already knows where every file is (and whether it is gone), the relinker
already knows how to tell "moved" from "renamed", and this module turns both into
one honest set of numbers for the Playlists page and the missing-files report.

Why it exists (TimeTrax 996 / 1036 / 1052, Mark 2026-11-19): after the NAS was
reorganised, 241 of 366 songs on the on-air playlist went dark and the station
played five leftover cache clips for 1h41m. The index knew — the screen did not.
The rule that makes the numbers trustworthy: an index row flagged `missing=1` was
PROVEN gone by the indexer (its folder was readable), while a row that is simply
absent from the index is NOT evidence of death. Never collapse those two.

Measured on the on-air box 2026-11-19 (see docs/LIBRARY-HEALTH-CONSENSUS.md):
358,987 tracks, 264,945 flagged, 13,837 playlist items, 1,085 unplayable items —
and an exact-path join reports 93% of items dead because the index and the
playlists spell the same path with different separators. Every comparison here
goes through `normalise` (and through the operator's path aliases first).
"""

from __future__ import annotations

import os
import sqlite3
import time

from . import playlists as pl

# A well-known junk bucket: never offer a match from inside it.
QUARANTINE = pl.QUARANTINE_DIR

# Cache keyed on (db path, db mtime): the Playlists page polls while an operator
# looks at it, and rebuilding a 14k-item status map per poll would make the page
# feel broken. Any indexer write moves the mtime and refreshes it.
_CACHE: dict = {"key": None, "at": 0.0, "data": None}
_CACHE_TTL = 5.0        # short: an operator who just deleted an item must not
                        # stare at the old count while the page polls


def normalise(path: str) -> str:
    """The comparison key for a path: case- and separator-insensitive.

    The index stores `Z:/G\\Artist\\x.mp3` and the playlists store
    `Z:\\G\\Artist\\x.mp3` for the SAME file; an exact join calls that file
    missing (93% of the on-air box's playlist items, measured 2026-11-19).
    """
    return os.path.normcase(os.path.normpath((path or "").strip())) \
        .replace("/", "\\")


def reachable(cfg: dict) -> bool:
    """One `isdir` probe of the music root — the only share I/O this module does.

    False means "we cannot see the library right now", which must never be
    reported as files having disappeared.
    """
    root = ((cfg or {}).get("nas_music_root") or "").strip()
    return bool(root) and os.path.isdir(pl.alias_path(root))


# --------------------------------------------------------------- index view

def _wanted(conn) -> tuple[list[dict], set[str], set[str]]:
    """Every playlist file item, plus the normalised + basename keys they need."""
    items = [dict(r) for r in conn.execute(
        "SELECT i.id, i.playlist_id, i.item_type, i.path, i.title, i.position, "
        "       p.name AS playlist_name "
        "  FROM playlist_items i JOIN playlists p ON p.id = i.playlist_id "
        " ORDER BY i.playlist_id, i.position")]
    paths, names = set(), set()
    for it in items:
        if it["item_type"] != "file":
            continue
        resolved = pl.alias_path(it["path"])
        it["resolved"] = resolved
        paths.add(normalise(resolved))
        names.add(pl._file_key(it["path"]))
    return items, paths, names


def _index_scan(conn, wanted: set[str], names: set[str]) -> tuple[dict, dict]:
    """ONE streaming pass over `tracks`, keeping only what the playlists need.

    Memory stays bounded by the number of playlist items (~14k), not the size of
    the library (359k rows on the on-air box) — which is what makes this safe to
    run from a web request while the station is on air.
    """
    found: dict[str, dict] = {}          # normalised path -> row
    by_name: dict[str, list] = {}        # _file_key -> PRESENT candidates
    for row in conn.execute(
            "SELECT path, missing, size, duration_sec FROM tracks"):
        key = normalise(row["path"])
        if key in wanted:
            found[key] = {"missing": row["missing"] or 0, "size": row["size"],
                          "duration_sec": row["duration_sec"]}
        if not row["missing"]:
            nk = pl._file_key(row["path"])
            if nk in names:
                by_name.setdefault(nk, []).append(
                    {"path": row["path"], "size": row["size"],
                     "duration_sec": row["duration_sec"],
                     "parts": pl._path_parts(row["path"])})
    return found, by_name


def status_of(it: dict, found: dict) -> str:
    """'playable' | 'missing' | 'not_indexed'.

    `missing` only when the index holds the row AND flags it gone. A path the
    index has never seen is 'not_indexed' — not dead.
    """
    if it.get("item_type") != "file":
        return "folder"
    hit = found.get(normalise(it.get("resolved") or pl.alias_path(it["path"])))
    if hit is None:
        return "not_indexed"
    return "missing" if hit["missing"] else "playable"


def _match_hint(it: dict, hit: dict | None, by_name: dict) -> tuple[str, str | None]:
    """Would the relinker call this 'moved'? Uses ITS ladder, not a second one.

    Returns (verdict, candidate) where verdict is
      'moved'     — unique winner at depth >= 2, or unique same-size candidate
      'ambiguous' — several candidates and no decisive winner
      'conflict'  — one candidate, but a different recording (size+duration differ)
      'none'      — no candidate at all
    A hint is never applied automatically; only the operator's Fix-paths button
    rewrites anything.
    """
    cands = [c for c in (by_name.get(pl._file_key(it["path"])) or ())
             if normalise(c["path"]) != normalise(it.get("resolved") or it["path"])
             and QUARANTINE not in normalise(c["path"])]
    if not cands:
        return "none", None
    it_parts = pl._path_parts(it["path"])
    scored = [(pl._suffix_depth(it_parts, c["parts"]), c) for c in cands]
    best = max(d for d, _ in scored)
    winners = [c for d, c in scored if d == best]
    dead = hit or {}
    if len(winners) > 1:
        # size is the only tie-break the relinker trusts
        same = [c for c in winners if dead.get("size") is not None
                and c["size"] == dead["size"]]
        if len(same) == 1:
            return "moved", same[0]["path"]
        return "ambiguous", None
    winner = winners[0]
    if best >= 2:
        return "moved", winner["path"]
    if pl._different_recording(dead, winner):
        return "conflict", None
    if dead.get("size") is not None and winner["size"] == dead["size"]:
        return "moved", winner["path"]
    return "ambiguous", None


# ------------------------------------------------------------------ views

def _cached(conn, cfg, fresh: bool = False):
    key = None
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        db_path = row[2] if row else None
        if db_path:
            # The write may land in a journal/WAL file rather than the main db,
            # so the key covers the sidecar files too — otherwise a playlist
            # edit made a second ago reads as "no change" and the operator sees
            # stale counts.
            stamp = []
            for suffix in ("", "-wal", "-journal"):
                try:
                    st = os.stat(db_path + suffix)
                    stamp.append((st.st_mtime, st.st_size))
                except OSError:
                    stamp.append(None)
            key = (db_path, tuple(stamp))
    except (OSError, sqlite3.Error):
        key = None
    now = time.time()
    if (not fresh and key is not None and _CACHE["key"] == key
            and now - _CACHE["at"] < _CACHE_TTL and _CACHE["data"]):
        return _CACHE["data"], True

    items, wanted, names = _wanted(conn)
    found, by_name = _index_scan(conn, wanted, names)
    # NOTE: reachability is deliberately NOT cached — the item statuses are
    # the expensive part and do not depend on the share; whether the share is
    # up right now is a per-call fact (a report must not claim "reachable"
    # because it was ten seconds ago).
    data = {"items": items, "found": found, "by_name": by_name}
    if key is not None:
        _CACHE.update({"key": key, "at": now, "data": data})
    return data, False


def _settings_ids(conn) -> tuple[int | None, int | None]:
    rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    def _int(k):
        try:
            return int(rows.get(k) or 0) or None
        except (TypeError, ValueError):
            return None
    return _int("active_playlist_id"), _int("emergency_playlist_id")


def health(conn, cfg, fresh: bool = False) -> dict:
    """The Playlists page in one call: per-playlist counts + the index snapshot.

    Per playlist: items / playable / missing / not indexed / folders / updated /
    last aired. `unplayable` is `missing` — the index PROVED those gone; the
    unindexed column is shown beside it, never added to it, because "not in the
    index" is not evidence of death.
    """
    data, cached = _cached(conn, cfg, fresh)
    items, found = data["items"], data["found"]
    active, emergency = _settings_ids(conn)

    air = {}
    try:
        for r in conn.execute(
                "SELECT playlist_id, MAX(started_at) AS last FROM playlist_airings "
                "WHERE playlist_id IS NOT NULL GROUP BY playlist_id"):
            air[r["playlist_id"]] = r["last"]
    except sqlite3.Error:
        air = {}

    rows, per = [], {}
    for it in items:
        agg = per.setdefault(it["playlist_id"], {
            "items": 0, "playable": 0, "missing": 0, "not_indexed": 0,
            "folders": 0, "paths_missing": set()})
        if it["item_type"] == "file":
            agg["items"] += 1
            st = status_of(it, found)
            if st == "playable":
                agg["playable"] += 1
            elif st == "missing":
                agg["missing"] += 1
                agg["paths_missing"].add(normalise(it["resolved"]))
            else:
                agg["not_indexed"] += 1
        else:
            agg["folders"] += 1

    for p in conn.execute("SELECT id, name, updated_at FROM playlists ORDER BY id"):
        agg = per.get(p["id"]) or {"items": 0, "playable": 0, "missing": 0,
                                   "not_indexed": 0, "folders": 0,
                                   "paths_missing": set()}
        total = agg["items"]
        rows.append({
            "id": p["id"], "name": p["name"],
            "items": total, "playable": agg["playable"],
            "missing": agg["missing"], "not_indexed": agg["not_indexed"],
            "folders": agg["folders"],
            "pct_unplayable": round(agg["missing"] / total * 100, 1) if total else 0.0,
            "updated_at": p["updated_at"],
            "last_aired": air.get(p["id"]),
            "on_air": p["id"] == active,
            "emergency": p["id"] == emergency,
        })

    # pin what is on air (and the fallback), then worst-first
    rows.sort(key=lambda r: (not (r["on_air"] or r["emergency"]),
                             -r["missing"], -r["pct_unplayable"],
                             r["name"].lower()))
    is_up = reachable(cfg)
    distinct_missing = set()
    for agg in per.values():
        distinct_missing |= agg["paths_missing"]

    snap = _snapshot(conn)
    snap["playlist_missing_paths"] = len(distinct_missing)
    snap["reachable"] = is_up
    return {"paused": False, "cached": cached, "snapshot": snap,
            "reachable": is_up,
            "playlists": rows,
            "active_playlist_id": active, "emergency_playlist_id": emergency}


def _snapshot(conn) -> dict:
    """The calm index card: is the library there, how much music, when scanned."""
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    total = q("SELECT COUNT(*) FROM tracks") or 0
    missing = q("SELECT COUNT(*) FROM tracks WHERE missing=1") or 0
    present = total - missing
    secs = q("SELECT COALESCE(SUM(duration_sec),0) FROM tracks WHERE missing=0") or 0
    size = q("SELECT COALESCE(SUM(size),0) FROM tracks WHERE missing=0") or 0
    last_scan = q("SELECT MAX(indexed_at) FROM tracks")
    newest = q("SELECT MAX(mtime) FROM tracks WHERE missing=0")
    oldest = q("SELECT MIN(mtime) FROM tracks WHERE missing=0")
    fmts = [{"format": (r["format"] or "unknown"), "n": r["n"]}
            for r in conn.execute(
                "SELECT COALESCE(format,'unknown') AS format, COUNT(*) AS n "
                "FROM tracks WHERE missing=0 GROUP BY 1 ORDER BY n DESC LIMIT 5")]
    return {"tracks_total": total, "tracks_missing": missing,
            "tracks_present": present,
            "hours_present": round(secs / 3600.0, 1),
            "bytes_present": int(size),
            "last_scan": last_scan, "newest_file": newest, "oldest_file": oldest,
            "formats": fmts}


def missing(conn, cfg, fresh: bool = False) -> dict:
    """Every playlist item that will not play, grouped by the folder it lived in.

    Grouped by PARENT FOLDER, not by artist: the failure this answers was folder
    surgery on the NAS, and a folder is what pairs with a recycle-bin restore.
    """
    data, _ = _cached(conn, cfg, fresh)
    items, found, by_name = data["items"], data["found"], data["by_name"]
    rows = []
    for it in items:
        if it["item_type"] != "file":
            continue
        st = status_of(it, found)
        if st == "playable":
            continue
        hit = found.get(normalise(it["resolved"]))
        verdict, cand = _match_hint(it, hit, by_name) if st == "missing" \
            else ("none", None)
        folder = os.path.dirname((it["resolved"] or it["path"])
                                 .replace("/", "\\"))
        rows.append({
            "playlist_id": it["playlist_id"], "playlist": it["playlist_name"],
            "path": it["path"], "resolved": it["resolved"],
            "title": it["title"] or os.path.basename(it["path"].replace("/", "\\")),
            "folder": folder, "status": st, "hint": verdict,
            "candidate": cand,
        })

    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["folder"], []).append(r)
    out = [{"folder": f, "files": sorted(v,
                                         key=lambda x: x["title"].lower())}
           for f, v in sorted(groups.items(),
                              key=lambda kv: (-len(kv[1]), kv[0].lower()))]
    return {"reachable": reachable(cfg), "groups": out,
            "files": len(rows),
            "paths": len({normalise(r["resolved"]) for r in rows}),
            "missing": sum(1 for r in rows if r["status"] == "missing"),
            "not_indexed": sum(1 for r in rows if r["status"] == "not_indexed"),
            "moved_hints": sum(1 for r in rows if r["hint"] == "moved"),
            "scanned": (_snapshot(conn) or {}).get("last_scan")}


def playlist_items_flags(conn, pid: int) -> dict[int, str]:
    """item id -> status, for the editor's row marks (single playlist, cheap)."""
    data, _ = _cached(conn, None)
    out = {}
    for it in data["items"]:
        if it["playlist_id"] == pid and it["item_type"] == "file":
            out[it["id"]] = status_of(it, data["found"])
    return out


# ------------------------------------------------------------------- routes

def register(app) -> None:
    from fastapi import Depends, HTTPException

    get_conn = app.state.get_conn
    api_user = app.state.api_user
    cfg = app.state.cfg

    @app.get("/api/library/health")
    def api_health(fresh: int = 0, conn=Depends(get_conn), _=Depends(api_user)):
        return health(conn, cfg, bool(fresh))

    @app.get("/api/library/missing")
    def api_missing(fresh: int = 0, conn=Depends(get_conn), _=Depends(api_user)):
        return missing(conn, cfg, bool(fresh))

    @app.get("/api/library/playlist/{pid}/flags")
    def api_flags(pid: int, conn=Depends(get_conn), _=Depends(api_user)):
        return {"flags": playlist_items_flags(conn, pid)}
