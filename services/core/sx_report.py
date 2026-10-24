"""SoundExchange Report of Use — built from StudioFire's own as-aired log.

WHY THIS EXISTS
---------------
KDPI (Drop-in Radio) simulcasts its FM programme on the public web stream, which
is hosted by StreamGuys. That makes the station a *nonsubscription transmission
service* under 17 U.S.C. 114(d)(2), and 37 CFR 370.4(d)(2) requires a monthly
**Report of Use** listing, for every sound recording transmitted:

    (i)   the name of the service
    (ii)  the category transmission code
    (iii) the featured artist
    (iv)  the sound recording title
    (v)   the ISRC or, alternatively, the album title AND marketing label
    (vi)  the actual total performances during the reporting period

Reports are due 45 days after the close of each calendar month (370.4(c)) and
must be retained for 3 years (370.4(d)(6)).


THE PROBLEM THIS MODULE SOLVES
------------------------------
`play_history` does NOT store the library path. The engine journal records the
**precache** copy it actually fed to mpv, i.e.

    C:\\StudioFire\\StudioFire-OnAir-v1.0.0\\precache\\21f14d911e6855d3.m4a

and the precache filename is a deterministic function of the source path
(`engine_bridge.Precache.cache_path_for`):

    sha1(normcase(abspath(src)))[:16] + original extension

So the library file is recoverable from the as-aired row, for the whole
history, with no engine change and no schema migration: hash every row in
`tracks` once and match on the digest. Verified against live on-air data
2026-10-24 (3/3 spot checks matched exactly).

That join is what turns "Artist - Song" strings into reportable records with
album / duration / format, and (via an optional mutagen tag read) ISRC and
marketing label.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* **It does not invent "actual total performances".** 370.4(d)(2)(vi) is
  performances *to listeners*, which requires the listener side — StreamGuys'
  connection logs, or a listener-series file. If no listener series is given
  the ATP column is left empty and the run reports the exact number of rows
  awaiting that join. It never substitutes play count for performances.
* **It never touches P1.** Read-only against the core DB; stdlib + optional
  mutagen only; safe to run on the on-air box.
* **It never writes to the live DB.**

CLI
---
    python -m services.core.sx_report --month 2026-09 --out reports/
    python -m services.core.sx_report --from 2026-09-01 --to 2026-09-30 \
        --listeners listeners.csv --out reports/
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import sys

# Sources in play_history that carry a *sound recording* under the statutory
# licence. 'spot' (ads / underwriting / IDs / PSAs) is production, not a
# licensed sound recording performance; the sine tone is not a recording.
MUSIC_SOURCES = ("playlist", "show", "manual", "emergency")

SERVICE_NAME_DEFAULT = "KDPI Drop-In Radio, Inc."
# 370.4(d)(2)(ii)(B): eligible nonsubscription transmissions of broadcast
# simulcast programming not reasonably classified as news/talk/sports/business.
# KDPI simulcasts its FM programme; talk-heavy hours are a separate question for
# the station (see the report's warnings file).
CATEGORY_CODE_DEFAULT = "B"

SX_COLUMNS = [
    "Service Name",
    "Category Transmission Code",
    "Featured Artist",
    "Sound Recording Title",
    "ISRC",
    "Album Title",
    "Marketing Label",
    "Actual Total Performances",
]

TRACKLOG_COLUMNS = [
    "start_local",
    "start_utc",
    "artist",
    "title",
    "album",
    "isrc",
    "marketing_label",
    "duration_sec",
    "source",
    "library_path",
    "resolve",
    "journal_id",
]


# --------------------------------------------------------------- precache key

def precache_digest(path: str) -> str:
    """The 16-hex-char precache digest for a source path.

    MUST stay byte-identical to `engine_bridge.Precache.cache_path_for`. If that
    ever changes, this function and `tests/test_sx_report.py` change with it.
    """
    return hashlib.sha1(
        os.path.normcase(os.path.abspath(path)).encode()).hexdigest()[:16]


def digest_from_precache_path(precache_path: str) -> str | None:
    """Pull the digest out of `...\\precache\\<digest>.<ext>`. None if it is not
    a precache path (e.g. the `av://lavfi:sine=...` failover tone)."""
    if not precache_path:
        return None
    norm = precache_path.replace("/", "\\")
    if "\\precache\\" not in norm.lower():
        return None
    base = norm.rsplit("\\", 1)[-1]
    stem = base.split(".", 1)[0]
    if len(stem) == 16 and all(c in "0123456789abcdef" for c in stem.lower()):
        return stem.lower()
    return None


# ------------------------------------------------------------------- indexing

def build_precache_index(conn, nas_music_root: str | None = None) -> dict:
    """digest -> track row. One pass over `tracks` (~345k rows, a few seconds).
    Built in memory per run: no migration, no write to the live DB.

    ⚠️ **A single digest per library path is not enough.** The digest is taken
    over the path *as the feeder handed it to the engine*, and the same file
    reaches the feeder in more than one shape: the index stores the NAS form
    (`//KDPI-Media/music/G\\...`) while playback frequently hands the engine the
    drive-letter form (`Z:/G\\...`, Z: being the mapping of the same share).
    Measured on live October 2026 data: hashing only the stored form resolved
    87.6% of performances; adding the drive-letter variant resolves 100% of
    the ones we spot-checked. So hash every equivalent form of the path.
    """
    idx: dict = {}
    share = None
    if nas_music_root:
        try:
            from .. import pathmap
            share = pathmap.share_root(nas_music_root)
        except Exception:
            share = None
    for row in conn.execute(
            "SELECT path, title, artist, album, duration_sec, format FROM tracks"):
        path = row["path"]
        if not path:
            continue
        meta = {
            "path": path,
            "title": row["title"],
            "artist": row["artist"],
            "album": row["album"],
            "duration_sec": row["duration_sec"],
            "format": row["format"],
        }
        for form in _path_variants(path, share):
            idx.setdefault(precache_digest(form), meta)
    return idx


def _path_variants(path: str, share_root: str | None) -> set:
    """Every path shape the engine may have been handed for this file.

    * the stored form
    * `Z:` + <path minus the share root>  — the drive-letter form the feeder
      uses when the mapping exists (Z: is the documented music-share letter;
      `config\\drive-map.bat` is the authority, see kdpi-radio-network)
    """
    out = {path}
    if share_root:
        norm = path.replace("/", "\\")
        sr = share_root.replace("/", "\\").rstrip("\\")
        for cand in (sr, sr.replace("\\\\192.168.6.146\\music", "\\\\KDPI-Media\\music")):
            if norm.lower().startswith(cand.lower() + "\\"):
                remainder = norm[len(cand):]
                out.add("Z:" + remainder)
                break
    return out


# ---------------------------------------------------------------- collecting

def _split_title(display: str | None) -> tuple[str | None, str | None]:
    """The journal title is a single 'Artist - Song' string (em dash and en
    dash variants have been seen). Returns (artist, title); ('', display) when
    there is no separator, so nothing is silently dropped."""
    if not display:
        return None, None
    for sep in (" - ", " — ", " – "):
        if sep in display:
            artist, title = display.split(sep, 1)
            return artist.strip(), title.strip()
    return None, display.strip()


def collect_period(conn, start: dt.datetime, end: dt.datetime,
                   sources=MUSIC_SOURCES, index: dict | None = None) -> list:
    """Every licensed sound-recording performance between `start` (inclusive)
    and `end` (exclusive). `start`/`end` must be timezone-aware.

    play_history.ts is an ISO-8601 string with an offset, so the comparison is
    done on the parsed datetime rather than a string range (a string range
    silently mixes offsets).
    """
    idx = index if index is not None else build_precache_index(conn)
    marks = ",".join("?" * len(sources))
    out = []
    for row in conn.execute(
            "SELECT journal_id, ts, path, title, source FROM play_history "
            f"WHERE event = 'track_start' AND source IN ({marks})", tuple(sources)):
        ts = _parse_ts(row["ts"])
        if ts is None or not (start <= ts < end):
            continue
        digest = digest_from_precache_path(row["path"])
        meta = idx.get(digest) if digest else None
        artist = (meta or {}).get("artist")
        title = (meta or {}).get("title")
        if not artist and not title:
            artist, title = _split_title(row["title"])
        out.append({
            "journal_id": row["journal_id"],
            "start": ts,
            "artist": artist or "",
            "title": title or "",
            "album": (meta or {}).get("album") or "",
            "duration_sec": (meta or {}).get("duration_sec"),
            "source": row["source"],
            "library_path": (meta or {}).get("path") or "",
            "isrc": "",
            "marketing_label": "",
            "resolve": "indexed" if meta else ("unmatched" if digest else "no-digest"),
            "listeners": None,
        })
    out.sort(key=lambda r: r["start"])
    return out


def _parse_ts(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        ts = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts


# ----------------------------------------------------------------- tag lookup

def read_tags(path: str) -> dict:
    """ISRC + marketing label from the file's own tags, if present.

    Tag vocabularies differ per container, hence the key sweep:
      * ID3 (mp3/wav):      TSRC (ISRC), TPUB (publisher/label)
      * Vorbis (flac/ogg):  'isrc', 'label'/'organization'/'publisher'
      * MP4/M4A:            '----:com.apple.iTunes:ISRC', '©pub'/'©lab'
    Missing mutagen, unreadable file or absent tags => empty strings. Nothing
    here may ever raise: a tag read failing must not lose a performance.
    """
    out = {"isrc": "", "marketing_label": ""}
    try:
        import mutagen  # imported lazily: optional dependency
        f = mutagen.File(path, easy=False)
    except Exception:
        return out
    if f is None or f.tags is None:
        return out
    try:
        items = list(f.tags.items())
    except Exception:
        return out
    for key, val in items:
        k = str(key).upper()
        try:
            v = val[0] if isinstance(val, (list, tuple)) and val else val
            v = str(v).strip()
        except Exception:
            continue
        if not v:
            continue
        if ("ISRC" in k or k == "TSRC") and not out["isrc"]:
            out["isrc"] = v
        elif ("TPUB" in k or "LABEL" in k or "ORGANIZATION" in k
              or "PUBLISHER" in k or "©PUB" in k or "©LAB" in k):
            if not out["marketing_label"]:
                out["marketing_label"] = v
    return out


def enrich_tags(plays: list, cache_path: str | None = None,
                resolve=None) -> dict:
    """Fill `isrc` / `marketing_label` on each play by reading the library
    file's tags. `resolve(path)` maps a stored path to something openable on
    this host (on the boxes that is `pathmap.resolve`). Results are cached on
    disk by path+size+mtime so repeated runs cost one stat per file.

    Returns a small stats dict; never raises.
    """
    stats = {"cache_hit": 0, "read": 0, "unreadable": 0, "with_isrc": 0,
             "with_label": 0}
    cache = {}
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = {}
    dirty = False
    for play in plays:
        src = play.get("library_path")
        if not src:
            continue
        openable = resolve(src) if resolve else src
        key = None
        try:
            st = os.stat(openable)
            key = f"{src}|{st.st_size}|{int(st.st_mtime)}"
        except OSError:
            stats["unreadable"] += 1
            continue
        hit = cache.get(key)
        if hit is None:
            hit = read_tags(openable)
            cache[key] = hit
            dirty = True
            stats["read"] += 1
        else:
            stats["cache_hit"] += 1
        play["isrc"] = hit.get("isrc", "")
        play["marketing_label"] = hit.get("marketing_label", "")
        if play["isrc"]:
            stats["with_isrc"] += 1
        if play["marketing_label"]:
            stats["with_label"] += 1
    if cache_path and dirty:
        try:
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
            os.replace(tmp, cache_path)
        except OSError:
            pass
    return stats


# --------------------------------------------------------------- listener join

def load_listener_series(path: str) -> list:
    """Read a listener series: CSV with `ts,listeners` (ISO-8601 timestamp and
    the concurrent listener count at that moment). This is the StreamGuys side
    of the join — their connection logs, or our own sampling of
    /status-json.xsl. Returns [(aware datetime, int)] sorted by time."""
    series = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            ts = _parse_ts((row.get("ts") or "").strip())
            raw = (row.get("listeners") or row.get("count") or "").strip()
            if ts is None or not raw:
                continue
            try:
                series.append((ts, int(float(raw))))
            except ValueError:
                continue
    series.sort(key=lambda x: x[0])
    return series


def _mean_listeners(series: list, start: dt.datetime, end: dt.datetime):
    """Mean concurrent listeners over [start, end) from the sampled series.
    None when the series does not cover the window (never guess)."""
    if not series:
        return None
    vals = [n for ts, n in series if start <= ts < end]
    if not vals:
        return None
    return sum(vals) / len(vals)


def apply_listeners(plays: list, series: list) -> int:
    """Attach a mean listener count to each play. Returns how many plays are
    still without a listener figure."""
    missing = 0
    for play in plays:
        end = play["start"] + dt.timedelta(
            seconds=float(play.get("duration_sec") or 0) or 1.0)
        n = _mean_listeners(series, play["start"], end)
        play["listeners"] = n
        if n is None:
            missing += 1
    return missing


# --------------------------------------------------------------- aggregation

def aggregate(plays: list, service_name: str = SERVICE_NAME_DEFAULT,
              category_code: str = CATEGORY_CODE_DEFAULT) -> list:
    """Collapse performances into Report-of-Use rows, keyed on the identity the
    regulation recognises: ISRC when we have it, else artist+title+album+label
    (two different recordings of the same song must not merge)."""
    groups: dict = {}
    for play in plays:
        artist = (play.get("artist") or "").strip()
        title = (play.get("title") or "").strip()
        if not title:
            continue  # unidentifiable performance — surfaced in warnings
        if play.get("isrc"):
            key = ("isrc", play["isrc"].upper())
        else:
            key = ("meta", artist.lower(), title.lower(),
                   (play.get("album") or "").lower(),
                   (play.get("marketing_label") or "").lower())
        g = groups.setdefault(key, {
            "artist": artist,
            "title": title,
            "isrc": play.get("isrc") or "",
            "album": play.get("album") or "",
            "label": play.get("marketing_label") or "",
            "plays": 0,
            "performances": 0.0,
            "listener_complete": True,
        })
        g["plays"] += 1
        if play.get("listeners") is None:
            g["listener_complete"] = False
        else:
            g["performances"] += float(play["listeners"])
    rows = []
    for g in groups.values():
        rows.append({
            "Service Name": service_name,
            "Category Transmission Code": category_code,
            "Featured Artist": g["artist"],
            "Sound Recording Title": g["title"],
            "ISRC": g["isrc"],
            "Album Title": g["album"],
            "Marketing Label": g["label"],
            "Actual Total Performances": (
                int(round(g["performances"])) if g["listener_complete"] else ""),
            "_plays": g["plays"],
        })
    rows.sort(key=lambda r: (r["Featured Artist"].lower(),
                             r["Sound Recording Title"].lower()))
    return rows


# ------------------------------------------------------------------- writers

def write_tracklog(plays: list, path: str) -> int:
    """The detail / audit file: one row per performance, local + UTC start,
    library path and how it was resolved. This is the artifact to hand a
    reporting vendor that owns the listener side of the join, and the evidence
    we keep for the 3-year retention rule."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TRACKLOG_COLUMNS)
        w.writeheader()
        for p in plays:
            w.writerow({
                "start_local": p["start"].strftime("%Y-%m-%d %H:%M:%S"),
                "start_utc": p["start"].astimezone(dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                "artist": p["artist"],
                "title": p["title"],
                "album": p["album"],
                "isrc": p["isrc"],
                "marketing_label": p["marketing_label"],
                "duration_sec": p["duration_sec"] or "",
                "source": p["source"],
                "library_path": p["library_path"],
                "resolve": p["resolve"],
                "journal_id": p["journal_id"],
            })
    return len(plays)


def write_sx_report(rows: list, path: str) -> int:
    """The Report of Use itself: exactly the 370.4(d)(2) columns, in order."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SX_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def write_warnings(plays: list, rows: list, path: str, extra: dict) -> None:
    """Everything a human has to know before filing. Loud, plain, no hedging."""
    unidentified = [p for p in plays if not (p.get("title") or "").strip()]
    unmatched = [p for p in plays if p.get("resolve") != "indexed"]
    no_isrc = [p for p in plays if not p.get("isrc")]
    no_label = [p for p in plays if not p.get("marketing_label")]
    no_listener = [p for p in plays if p.get("listeners") is None]
    lines = [
        "StudioFire — SoundExchange Report of Use: coverage warnings",
        "=" * 68,
        f"performances collected : {len(plays)}",
        f"report rows (grouped)  : {len(rows)}",
        "",
        "370.4(d)(2)(v) — ISRC or (album title AND marketing label):",
        f"  rows with no ISRC           : {len(no_isrc)} / {len(plays)}",
        f"  rows with no label tag      : {len(no_label)} / {len(plays)}",
        "  -> a row is compliant only if it has the ISRC, or BOTH the album",
        "     title and the marketing label. SoundExchange can match on",
        "     artist/title/album, but the regulation asks for the fields.",
        "",
        "370.4(d)(2)(vi) — actual total performances:",
        f"  performances without a listener figure : {len(no_listener)} / {len(plays)}",
        "  -> play COUNT is not performances. Join StreamGuys' connection logs",
        "     (or a listener series via --listeners) before filing.",
        "",
        "identity resolution:",
        f"  identified via the library index : {len(plays) - len(unmatched)}",
        f"  NOT identified (title string only): {len(unmatched)}",
        f"  with no usable title             : {len(unidentified)}",
        "",
        "reporting period / category:",
        f"  period      : {extra.get('period', '')}",
        f"  category    : {extra.get('category', '')} "
        "(370.4(d)(2)(ii)(B) broadcast simulcast, music programming)",
        f"  due date    : {extra.get('due', '')} (45 days after period close)",
        "",
        "REMINDERS",
        "  * 370.4(d)(6): keep this report and its track log for 3 years.",
        "  * 370.4(d)(4): the report needs a signed statement by an officer of",
        "    the service — a human step, not something this tool can do.",
        "  * Only licensed sound recordings belong in this report; spots, IDs,",
        "    PSAs and underwriting are excluded by design.",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# ------------------------------------------------------------------------ CLI

def _month_bounds(month: str) -> tuple:
    y, m = (int(x) for x in month.split("-"))
    start = dt.datetime(y, m, 1, tzinfo=_local_tz())
    end = dt.datetime(y + (m // 12), (m % 12) + 1, 1, tzinfo=_local_tz())
    return start, end


def _local_tz():
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Build a SoundExchange Report of Use")
    ap.add_argument("--db", default=None, help="studiofire.db (default: config)")
    ap.add_argument("--month", help="calendar month, e.g. 2026-09")
    ap.add_argument("--from", dest="start", help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--to", dest="end", help="YYYY-MM-DD (exclusive)")
    ap.add_argument("--out", default="reports", help="output directory")
    ap.add_argument("--listeners", help="listener series CSV (ts,listeners)")
    ap.add_argument("--service", default=SERVICE_NAME_DEFAULT)
    ap.add_argument("--category", default=CATEGORY_CODE_DEFAULT)
    ap.add_argument("--no-tags", action="store_true",
                    help="skip the mutagen tag read (fast, leaves ISRC empty)")
    args = ap.parse_args(argv)

    tz = _local_tz()
    if args.month:
        start, end = _month_bounds(args.month)
    elif args.start and args.end:
        start = dt.datetime.fromisoformat(args.start).replace(tzinfo=tz)
        end = dt.datetime.fromisoformat(args.end).replace(tzinfo=tz)
    else:
        ap.error("give --month, or both --from and --to")

    from . import db as coredb  # local import so the module stays importable
    db_path = args.db
    if db_path is None:
        from .config import load_config
        cfg = load_config()
        db_path = cfg.get("paths", {}).get("db_path") or os.path.join(
            cfg.get("paths", {}).get("data_dir", "data"), "studiofire.db")
    conn = coredb.connect(db_path)

    nas_root = None
    try:
        from .config import load_config as _lc_idx
        nas_root = _lc_idx().get("paths", {}).get("nas_music_root")
    except Exception:
        nas_root = None
    index = build_precache_index(conn, nas_root)
    print(f"index: {len(index)} digests from the library")
    plays = collect_period(conn, start, end, index=index)
    print(f"performances in {start.date()}..{end.date()}: {len(plays)}")
    if not plays:
        print("nothing to report for that period")
        return 1

    tag_stats = {}
    if not args.no_tags:
        try:
            from services import pathmap
            from .config import load_config as _lc
            _cfg = _lc()
            aliases = _cfg.get("paths", {}).get("path_aliases") or {}
            root = _cfg.get("paths", {}).get("nas_music_root")
            resolve = lambda p: pathmap.resolve(p, aliases, root)  # noqa: E731
        except Exception:
            resolve = None
        cache = os.path.join(os.path.dirname(db_path), "sx_tag_cache.json")
        tag_stats = enrich_tags(plays, cache_path=cache, resolve=resolve)
        print(f"tags: {tag_stats}")

    listener_note = "no listener series supplied — ATP left blank"
    if args.listeners:
        series = load_listener_series(args.listeners)
        missing = apply_listeners(plays, series)
        listener_note = (f"{len(series)} listener samples; {missing} performances "
                         "outside their coverage")

    rows = aggregate(plays, args.service, args.category)
    os.makedirs(args.out, exist_ok=True)
    stem = args.month or f"{start.date()}_{end.date()}"
    p_report = os.path.join(args.out, f"sx-report-{stem}.csv")
    p_log = os.path.join(args.out, f"sx-tracklog-{stem}.csv")
    p_warn = os.path.join(args.out, f"sx-warnings-{stem}.txt")
    write_sx_report(rows, p_report)
    write_tracklog(plays, p_log)
    due = (end + dt.timedelta(days=45)).date()
    write_warnings(plays, rows, p_warn, {
        "period": f"{start.date()} .. {(end - dt.timedelta(days=1)).date()}",
        "category": args.category,
        "due": str(due),
    })
    print(f"wrote {p_report} ({len(rows)} rows)")
    print(f"wrote {p_log} ({len(plays)} performances)")
    print(f"wrote {p_warn}")
    print(f"listeners: {listener_note}")
    print(f"report due: {due} (45 days after month close)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
