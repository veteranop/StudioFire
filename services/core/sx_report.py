"""SoundExchange Report of Use — built from StudioFire's own as-aired log.

WHY THIS EXISTS
---------------
A station that simulcasts its FM programme on a public web stream (hosted by a
stream provider) is a *nonsubscription transmission service* under
17 U.S.C. 114(d)(2), and 37 CFR 370.4(d)(2) requires a monthly
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

    %ProgramData%\\StudioFire\\precache\\21f14d911e6855d3.m4a

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
  performances *to listeners*, which requires the listener side — the stream
  host's connection logs, or a listener-series file. If no listener series is given
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

# The service name is NEVER defaulted: it belongs to the station, and a report
# filed under another station's name is worse than no report. Configure it in
# config.json under royalty_reporting.service_name.
SERVICE_NAME_DEFAULT = ""
# 370.4(d)(2)(ii): A = webcast, B = broadcast simulcast (music), C = news/talk/
# sports/business programming. Default B, because a station that both transmits
# on FM and streams is a simulcast until it says otherwise.
CATEGORY_CODE_DEFAULT = "B"
CATEGORIES = ("A", "B", "C")
MODES = ("census_atp", "sample_atp", "sample_ath")

# Column order is fixed by the regulation, and differs between the performance
# modes (ATP) and the eligible-minimum-fee listening-hours mode (ATH).
FILING_COLUMNS = {
    "atp": ["Service Name", "Category Transmission Code", "Featured Artist",
            "Sound Recording Title", "ISRC", "Album Title", "Marketing Label",
            "Actual Total Performances"],
    "ath": ["Service Name", "Category Transmission Code", "Featured Artist",
            "Sound Recording Title", "ISRC", "Album Title", "Marketing Label",
            "Aggregate Tuning Hours", "Channel or Program Name",
            "Play Frequency"],
}

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

def build_precache_index(conn, share_roots=None,
                         drive_letter: str = "") -> dict:
    """digest -> track row. One pass over `tracks` (~345k rows, a few seconds).
    Built in memory per run: no migration, no write to the live DB.

    ⚠️ **A single digest per library path is not enough.** The digest is taken
    over the path *as the feeder handed it to the engine*, and the same file
    reaches the feeder in more than one shape: the index stores the NAS form
    (`//NAS/music/Library\\...`) while playback frequently hands the engine the
    drive-letter form (`Z:/Library\\...`, Z: being the mapping of the same share).
    Measured on live October 2026 data: hashing only the stored form resolved
    87.6% of performances; adding the drive-letter variant resolves 100% of
    the ones we spot-checked. So hash every equivalent form of the path.
    """
    idx: dict = {}
    # Accept either a share root ("\\NAS\music") or the music root inside it
    # ("\\NAS\music\Library"): pathmap.share_root() strips the library folder so the
    # drive-letter form keeps it, which is how the feeder wrote the path.
    roots = []
    for r in list(share_roots or []):
        rr = str(r or "")
        try:
            from .. import pathmap
            rr = pathmap.share_root(rr) or rr
        except Exception:          # noqa: BLE001 — best effort, never fatal
            pass
        if rr:
            roots.append(rr)
    # The library index can hold the same file twice (two scan vintages, one
    # with tags and one without). Taking the first row seen meant the royalty
    # report printed blank Featured Artist for plays whose file WAS tagged, so
    # keep whichever row carries the most metadata.
    has_label = any(r[1] == "label" for r in conn.execute(
        "PRAGMA table_info(tracks)"))
    cols = "path, title, artist, album, duration_sec, format"
    if has_label:
        cols += ", label"
    for row in conn.execute("SELECT " + cols + " FROM tracks"):
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
            "label": (row["label"] if has_label else None),
        }
        score = (bool(meta["artist"]), bool(meta["album"]),
                 bool(meta["label"]), bool(meta["title"]))
        for form in _path_variants(path, roots, drive_letter):
            digest = precache_digest(form)
            have = idx.get(digest)
            if have is None or score > have.get("_score", (False,) * 4):
                meta["_score"] = score
                idx[digest] = meta
    return idx


def _path_variants(path: str, share_roots, drive_letter: str = "") -> set:
    """Every path shape the engine may have been handed for this file.

    * the stored form, and
    * the drive-letter form, if the station maps the music share to a letter.

    `share_roots` is a list of equivalent share roots (a NAS can be reachable as
    a hostname and as a DNS alias); `drive_letter` is whatever the station's own
    drive mapping uses. Both come from config — nothing about one station's
    network belongs in this module.
    """
    out = {path}
    if not path:
        return out
    norm = path.replace("/", "\\")
    for root in (share_roots or []):
        sr = str(root or "").replace("/", "\\").rstrip("\\")
        if sr and norm.lower().startswith(sr.lower() + "\\"):
            rest = norm[len(sr):]
            if drive_letter:
                out.add(drive_letter.rstrip("\\") + rest)
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
            # (v) allows ISRC OR (album AND marketing label). The album comes
            # from the library row; the label now comes from the tag the
            # indexer stores (migration 16), so a filing does not depend on
            # re-reading every file off the share.
            "marketing_label": (meta or {}).get("label") or "",
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
    the concurrent listener count at that moment). This is the stream host's side
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
        "  -> play COUNT is not performances. Join the stream host's connection logs",
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



# ============================================================ filing format
# 37 CFR 370.4 asks for a plain-ASCII, delimited text file with its own layout,
# not a spreadsheet. "|" separates fields, "^" quotes text, numbers stay bare.
# The column set differs between the performance modes and the eligible-minimum-
# fee listening-hours mode, and the file name carries the service and dates.

SAMPLE_WEEK_DAYS = 7
DELIMITER = "|"
TEXT_IND = "^"


def _fold(value: str) -> str:
    """The same accent simplifier the now-playing line uses (Bjork, not Björk)."""
    try:
        from .nowplaying_file import fold
        return fold(str(value or ""))
    except Exception:          # noqa: BLE001 — a missing helper must not stop a filing
        return str(value or "")


def clean_ascii(value) -> tuple[str, list[str]]:
    """Printable ASCII only; "|" and "^" become spaces (they are delimiters).

    Returns the cleaned text and the characters that could not be represented at
    all, so the caller can say exactly which fields were touched. Accented
    letters are not "dropped": the now-playing fold maps them (Björk -> Bjork),
    which is what a filing should carry. A character with no ASCII equivalent
    (a snowman, a CJK glyph) is dropped and reported.
    """
    import unicodedata
    original = str(value or "")
    folded = _fold(original)
    lost = []
    for ch in original:
        if ord(ch) < 128:
            continue
        decomposed = unicodedata.normalize("NFKD", ch)
        if not any(32 <= ord(c) <= 126 for c in decomposed):
            lost.append(ch)
    kept = []
    for ch in folded:
        code = ord(ch)
        if ch in (DELIMITER, TEXT_IND):
            kept.append(" ")
            lost.append(ch)
        elif 32 <= code <= 126:
            kept.append(ch)
        elif code in (9, 10, 13, 160):
            kept.append(" ")
        else:
            kept.append(" ")
            lost.append(ch)
    text = " ".join("".join(kept).split())
    return text, lost


def enc(value, notes=None, where="") -> str:
    """A text field: caret-wrapped, or ^^ when it is empty."""
    text, dropped = clean_ascii(value)
    if dropped and notes is not None:
        notes.append("%s: removed %r" % (where or "field", "".join(dropped)))
    if not text:
        if notes is not None:
            notes.append("%s: empty string written as ^^" % (where or "field"))
        return TEXT_IND + TEXT_IND
    return TEXT_IND + text + TEXT_IND


def bare(value, places=2) -> str:
    """A number or a date: written without quotes, as the layout requires."""
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        if abs(value - round(value)) < 0.005:
            return str(int(round(value)))
        return ("%%.%df" % places) % value
    return str(value)


def normalize_isrc(raw) -> tuple[str, str | None]:
    """ISRC is 12 alphanumerics: uppercase, no hyphens, no spaces."""
    text = str(raw or "").strip().upper().replace("-", "").replace(" ", "")
    if not text:
        return "", None
    if len(text) != 12 or not text.isalnum():
        return text, "ISRC %r is not 12 alphanumeric characters" % text
    return text, None


def service_slug(service_name: str) -> str:
    """The file-name form: ASCII letters and digits only."""
    cleaned, _ = clean_ascii(service_name)
    return "".join(c for c in cleaned if c.isalnum())


def filing_filename(service_name: str, start: dt.datetime,
                    end_inclusive: dt.datetime) -> str:
    return "%s%s-%s.txt" % (service_slug(service_name),
                            start.strftime("%Y%m%d"),
                            end_inclusive.strftime("%Y%m%d"))


# ----------------------------------------------------------------- periods
def quarter_bounds(quarter: str) -> tuple[dt.datetime, dt.datetime]:
    """'2026-Q4' -> (start, end_exclusive), in local time."""
    q = str(quarter or "").strip().upper().replace("-", "")
    if len(q) != 6 or not q.startswith("20") or q[4] != "Q":
        raise ValueError("quarter must look like 2026-Q4")
    year, qn = int(q[:4]), int(q[5])
    if qn not in (1, 2, 3, 4):
        raise ValueError("quarter must be Q1-Q4")
    first_month = (qn - 1) * 3 + 1
    start = dt.datetime(year, first_month, 1, tzinfo=_local_tz())
    if qn == 4:
        end = dt.datetime(year + 1, 1, 1, tzinfo=_local_tz())
    else:
        end = dt.datetime(year, first_month + 3, 1, tzinfo=_local_tz())
    return start, end


def sample_week(day: str, quarter_start: dt.datetime,
                quarter_end: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """Seven full local days from `day`, inside the quarter. Raises otherwise."""
    try:
        start = dt.datetime.fromisoformat(day).replace(tzinfo=_local_tz())
    except ValueError:
        raise ValueError("week start %r is not a date (YYYY-MM-DD)" % day)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=SAMPLE_WEEK_DAYS)
    if start < quarter_start or end > quarter_end:
        raise ValueError("the week starting %s is not wholly inside the quarter"
                         % day)
    return start, end


def validate_weeks(week1: str, week2: str, quarter: str):
    qs, qe = quarter_bounds(quarter)
    w1s, w1e = sample_week(week1, qs, qe)
    w2s, w2e = sample_week(week2, qs, qe)
    if w1s < w2e and w2s < w1e:
        raise ValueError("the two sample weeks overlap")
    return (w1s, w1e), (w2s, w2e)


# ------------------------------------------------------------- grouping rows
def group_rows(plays: list, mode: str, cfg: dict, ath=None) -> list:
    """One row per recording, keyed the way the regulation recognises identity:
    ISRC when present, else artist + title + album + label."""
    groups: dict = {}
    for play in plays:
        title = (play.get("title") or "").strip()
        if not title:
            continue
        isrc, _ = normalize_isrc(play.get("isrc"))
        artist = (play.get("artist") or "").strip()
        album = (play.get("album") or "").strip()
        label = (play.get("marketing_label") or "").strip()
        key = ("isrc", isrc) if isrc else ("meta", artist.lower(),
                                           title.lower(), album.lower(),
                                           label.lower())
        g = groups.setdefault(key, {"artist": artist, "title": title,
                                    "isrc": isrc, "album": album,
                                    "label": label, "plays": 0,
                                    "performances": 0.0, "listener_complete":
                                    True})
        g["plays"] += 1
        if play.get("listeners") is None:
            g["listener_complete"] = False
        else:
            g["performances"] += float(play["listeners"])
    rows = []
    for g in groups.values():
        g["ath"] = ath
        rows.append(g)
    rows.sort(key=lambda r: ((r["artist"] or "").lower(),
                             (r["title"] or "").lower()))
    return rows


# --------------------------------------------------------------- the writers
def _service_field(cfg: dict) -> tuple[str, list[str]]:
    """Service name, with the filer appended when somebody else signs."""
    service = (cfg.get("service_name") or "").strip()
    filer = (cfg.get("filer_name") or "").strip()
    notes = []
    if filer and filer.lower() != service.lower():
        notes.append("Service Name written as '%s (filed by %s)' — confirm this "
                     "is how SoundExchange should see it" % (service, filer))
        return "%s (filed by %s)" % (service, filer), notes
    return service, notes


def write_filing(path: str, rows: list, mode: str, cfg: dict,
                 start: dt.datetime, end_inclusive: dt.datetime,
                 generated: dt.datetime, headered: bool) -> dict:
    """Write the filing .txt exactly as 370.4 lays it out. Returns notes."""
    notes: list[str] = []
    verify: list[str] = []
    columns = FILING_COLUMNS["ath" if mode == "sample_ath" else "atp"]
    service, service_notes = _service_field(cfg)
    verify.extend(service_notes)
    category = str(cfg.get("category") or CATEGORY_CODE_DEFAULT).upper()
    verify.append("Category Transmission Code written as the bare letter %r — "
                  "confirm it matches how the station is licensed" % category)
    if mode == "sample_ath" and rows and rows[0].get("ath") is not None:
        verify.append("Aggregate Tuning Hours repeated on every row — supply "
                      "the figure from your stream host's report")

    header_lines: list[str] = []
    if headered:
        contact = cfg.get("contact") or {}
        fields = [service, contact.get("name") or "",
                  contact.get("street") or "",
                  contact.get("city_state_zip") or "",
                  contact.get("phone") or "", contact.get("email") or ""]
        header_lines = [clean_ascii(f)[0] for f in fields]
        header_lines += [start.strftime("%Y%m%d"),
                         end_inclusive.strftime("%Y%m%d"),
                         generated.strftime("%Y%m%d"), str(len(rows)),
                         TEXT_IND, DELIMITER, ""]
        header_lines.append(DELIMITER.join(
            enc(c, notes, "column name") for c in columns))

    out_lines: list[str] = []
    for r in rows:
        cells = [enc(service, notes, "service name"),
                 bare(category)]
        cells.append(enc(r["artist"], notes, "artist %r" % r["title"][:40]))
        cells.append(enc(r["title"], notes, "title"))
        isrc_val, isrc_warn = normalize_isrc(r.get("isrc"))
        if isrc_warn:
            notes.append("%s: %s" % (r["title"][:40], isrc_warn))
        cells.append(enc(isrc_val, notes, "ISRC"))
        cells.append(enc(r["album"], notes, "album %r" % r["title"][:40]))
        cells.append(enc(r["label"], notes, "label %r" % r["title"][:40]))
        if mode == "sample_ath":
            cells.append(bare(r.get("ath")))
            cells.append(enc(cfg.get("channel_name") or service, notes,
                             "channel name"))
            cells.append(bare(r.get("plays"), 0))
        else:
            cells.append(bare(r.get("performances")
                              if r.get("listener_complete") else None))
        out_lines.append(DELIMITER.join(cells))

    with open(path, "w", encoding="ascii", errors="strict", newline="") as fh:
        for line in header_lines + out_lines:
            fh.write(line + "\r\n")
    return {"rows": len(out_lines), "notes": notes, "verify": verify}


def write_review_csv(path: str, rows: list, mode: str, cfg: dict) -> int:
    """The same rows with a header, so a human can check them in Excel."""
    columns = FILING_COLUMNS["ath" if mode == "sample_ath" else "atp"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(columns)
        service, _ = _service_field(cfg)
        category = str(cfg.get("category") or CATEGORY_CODE_DEFAULT).upper()
        for r in rows:
            line = [service, category, r["artist"], r["title"], r["isrc"],
                    r["album"], r["label"]]
            if mode == "sample_ath":
                line += [bare(r.get("ath")), cfg.get("channel_name") or service,
                         r.get("plays")]
            else:
                line += [bare(r.get("performances"))
                         if r.get("listener_complete") else ""]
            w.writerow(line)
    return len(rows)


def write_attestation(path: str, service: str, filename: str) -> None:
    """The signed statement 370.4(d)(4) requires — a human fills this in."""
    body = (
        "SOUNDEXCHANGE REPORT OF USE — ATTESTATION\r\n"
        "===========================================\r\n\r\n"
        "I attest under penalty of perjury that the information in \r\n"
        "  %s\r\n"
        "is accurate to the best of my knowledge and belief, and that I am\r\n"
        "authorised to make this statement on behalf of %s.\r\n\r\n"
        "37 CFR 370.4(d)(4) requires this statement to be signed.\r\n\r\n"
        "Printed name : _______________________________________\r\n\r\n"
        "Title        : _______________________________________\r\n\r\n"
        "Signature    : _______________________________________\r\n\r\n"
        "Date         : _______________________________________\r\n"
    ) % (filename, service)
    with open(path, "w", encoding="ascii", errors="replace", newline="") as fh:
        fh.write(body)


# ---------------------------------------------------------------- pre-flight
def preflight(rows: list, plays: list, mode: str, cfg: dict, ath,
              notes: list, headered: bool) -> list:
    """Reasons the file is not ready. Empty list == READY TO FILE."""
    reasons: list[str] = []
    if not (cfg.get("service_name") or "").strip():
        reasons.append("service_name is not set in config.json "
                       "(royalty_reporting.service_name)")
    if headered:
        contact = cfg.get("contact") or {}
        missing = [k for k in ("name", "street", "city_state_zip", "phone",
                               "email") if not (contact.get(k) or "").strip()]
        if missing:
            reasons.append("the headered layout needs every contact field; "
                           "missing: " + ", ".join(missing))

    no_ident = [r for r in rows
                if not r["isrc"] and not (r["album"] and r["label"])]
    if no_ident:
        sample = "; ".join("%s — %s" % (r["artist"] or "(no artist)",
                                        r["title"]) for r in no_ident[:25])
        reasons.append("%d of %d rows have neither an ISRC nor BOTH album and "
                       "marketing label (first 25: %s)"
                       % (len(no_ident), len(rows), sample))

    if mode in ("census_atp", "sample_atp"):
        missing_atp = [r for r in rows if not r.get("listener_complete")]
        if missing_atp:
            reasons.append("%d of %d rows have no listener figure, so Actual "
                           "Total Performances cannot be filled. Supply a "
                           "listener CSV (--listeners) from your stream host, "
                           "or use the sample_ath mode if the station is an "
                           "eligible minimum-fee webcaster."
                           % (len(missing_atp), len(rows)))
    if mode == "sample_ath" and ath in (None, ""):
        reasons.append("sample_ath needs --ath (aggregate tuning hours from "
                       "your stream host)")

    empty_titles = [r for r in rows if not clean_ascii(r["title"])[0]]
    if empty_titles:
        reasons.append("%d rows have a title that is empty once cleaned to "
                       "ASCII" % len(empty_titles))

    hard_notes = [n for n in notes if "removed" in n]
    if hard_notes:
        reasons.append("%d fields needed non-ASCII or delimiter characters "
                       "stripped — check the affected rows before filing"
                       % len(hard_notes))
    return reasons


def write_warnings_file(path: str, ready: bool, reasons: list, plays: list,
                        rows: list, extra: dict, verify: list,
                        notes: list) -> None:
    """Line 1 is the verdict: READY TO FILE, or NOT READY and why."""
    unmatched = [p for p in plays if p.get("resolve") != "indexed"]
    unidentified = [p for p in plays if not (p.get("title") or "").strip()]
    no_isrc = [r for r in rows if not r["isrc"]]
    no_label = [r for r in rows if not r["label"]]
    lines = ["READY TO FILE" if ready else "NOT READY — fix these first"]
    if not ready:
        for i, reason in enumerate(reasons, 1):
            lines.append("  %d. %s" % (i, reason))
    lines += ["", "=" * 70,
              "performances collected : %d" % len(plays),
              "report rows (grouped)  : %d" % len(rows),
              "row count in the file  : %d" % extra.get("written", 0),
              "",
              "370.4(d)(2)(v) — ISRC or (album title AND marketing label):",
              "  rows with no ISRC      : %d / %d" % (len(no_isrc), len(rows)),
              "  rows with no label tag : %d / %d" % (len(no_label), len(rows)),
              "",
              "identity resolution:",
              "  identified via the library index : %d" % (len(plays) -
                                                              len(unmatched)),
              "  NOT identified (title string only): %d" % len(unmatched),
              "  with no usable title             : %d" % len(unidentified),
              "",
              "reporting period / category:",
              "  period   : %s" % extra.get("period", ""),
              "  mode     : %s" % extra.get("mode", ""),
              "  category : %s" % extra.get("category", ""),
              "  due date : %s (45 days after period close)"
              % extra.get("due", ""),
              ""]
    if verify:
        lines += ["# VERIFY — confirm these against SoundExchange's instructions:"]
        lines += ["  * " + v for v in verify]
        lines.append("")
    if notes:
        lines += ["character substitutions (ASCII / delimiter cleaning):"]
        lines += ["  * " + n for n in notes[:200]]
        if len(notes) > 200:
            lines.append("  ... and %d more" % (len(notes) - 200))
        lines.append("")
    lines += ["REMINDERS",
              "  * 370.4(d)(6): keep this report, its track log and the",
              "    attestation for 3 years.",
              "  * 370.4(d)(4): the report needs a signed statement — fill in",
              "    and sign the attestation file.",
              "  * Upload through SoundExchange Licensee Direct.",
              "  * The monthly Statement of Account (stream-host listening",
              "    hours and fees) is filed separately. StudioFire does not",
              "    produce it.",
              "  * Sample modes are only for an Eligible Minimum Fee Webcaster:",
              "    the station pays no more than the annual minimum fee AND",
              "    owns/operates an FCC-licensed AM/FM station. Confirm with",
              "    SoundExchange before filing this way.",
              "  * Only licensed sound recordings belong in this report; spots,",
              "    IDs, PSAs and underwriting are excluded by design.",
              ""]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\r\n".join(lines))


def build_zip(paths: list, zip_path: str) -> None:
    import zipfile
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in paths:
            zf.write(p, os.path.basename(p))


def build_report(*, conn, out_dir: str, cfg: dict, mode: str,
                 start: dt.datetime, end_excl: dt.datetime, plays: list,
                 category: str | None = None, ath=None, headered=None,
                 extra_note: str = "") -> dict:
    """Write the filing .txt + companions + zip. Returns the verdict.

    Never raises for a *data* problem: it writes every file, says what is
    missing, and hands back `ready`/`reasons`. A filing that silently omits
    rows is worse than one that refuses to call itself ready.
    """
    cfg = dict(cfg or {})
    if category:
        cfg["category"] = category
    if headered is None:
        headered = str(cfg.get("output_format") or "headerless") == "headered"

    now = dt.datetime.now(_local_tz())
    rows = group_rows(plays, mode, cfg, ath=ath)
    os.makedirs(out_dir, exist_ok=True)

    end_incl = end_excl - dt.timedelta(days=1)
    stem = service_slug(cfg.get("service_name") or "") or "report"
    stem = "%s%s-%s" % (stem, start.strftime("%Y%m%d"),
                        end_incl.strftime("%Y%m%d"))
    p_txt = os.path.join(out_dir, stem + ".txt")
    p_csv = os.path.join(out_dir, stem + "-review.csv")
    p_log = os.path.join(out_dir, stem + "-tracklog.csv")
    p_warn = os.path.join(out_dir, stem + "-warnings.txt")
    p_att = os.path.join(out_dir, stem + "-attestation.txt")
    p_zip = os.path.join(out_dir, stem + ".zip")

    written = write_filing(p_txt, rows, mode, cfg, start, end_incl, now,
                           headered)
    write_review_csv(p_csv, rows, mode, cfg)
    write_tracklog(plays, p_log)
    write_attestation(p_att, cfg.get("service_name") or "",
                      os.path.basename(p_txt))

    reasons = preflight(rows, plays, mode, cfg, ath, written["notes"], headered)
    ready = not reasons
    due = (end_excl + dt.timedelta(days=45)).date()
    write_warnings_file(p_warn, ready, reasons, plays, rows, {
        "period": "%s .. %s" % (start.date(), end_incl.date()),
        "mode": mode, "category": cfg.get("category"), "due": str(due),
        "written": written["rows"],
    }, written["verify"], written["notes"])

    build_zip([p_txt, p_csv, p_log, p_warn, p_att], p_zip)
    return {"ready": ready, "reasons": reasons, "rows": written["rows"],
            "grouped": len(rows), "performances": len(plays),
            "files": {"filing": p_txt, "review": p_csv, "tracklog": p_log,
                      "warnings": p_warn, "attestation": p_att, "zip": p_zip},
            "zip": p_zip, "stem": stem, "due": str(due), "mode": mode,
            "verify": written["verify"], "notes": written["notes"],
            "note": extra_note}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Build a SoundExchange Report of Use (37 CFR 370.4)")
    ap.add_argument("--db", default=None, help="studiofire.db (default: config)")
    ap.add_argument("--month", help="calendar month, e.g. 2026-09")
    ap.add_argument("--from", dest="start", help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--to", dest="end", help="YYYY-MM-DD (exclusive)")
    ap.add_argument("--quarter", help="YYYY-Qn (sample modes)")
    ap.add_argument("--week1", help="first sample week start, YYYY-MM-DD")
    ap.add_argument("--week2", help="second sample week start, YYYY-MM-DD")
    ap.add_argument("--ath", type=float,
                    help="aggregate tuning hours (required for sample_ath)")
    ap.add_argument("--out", default="reports", help="output directory")
    ap.add_argument("--listeners", help="listener series CSV (ts,listeners)")
    ap.add_argument("--service", help="override royalty_reporting.service_name")
    ap.add_argument("--mode", choices=MODES, help="census_atp | sample_atp | "
                                                  "sample_ath")
    ap.add_argument("--category", choices=CATEGORIES,
                    help="A webcast, B broadcast simulcast, C talk")
    ap.add_argument("--format", choices=("headerless", "headered"),
                    dest="fmt", help="filing layout")
    ap.add_argument("--no-tags", action="store_true",
                    help="skip the mutagen tag read (fast; leaves ISRC empty)")
    args = ap.parse_args(argv)

    from . import db as coredb
    from .config import load_config
    conf = load_config()
    cfg = dict(conf.get("royalty_reporting") or {})
    if args.service:
        cfg["service_name"] = args.service
    if not (cfg.get("service_name") or "").strip():
        print("Refusing to run: no service name configured.\n"
              "Set royalty_reporting.service_name in config/config.json "
              "(the name the station files under).")
        return 2

    mode = args.mode or cfg.get("mode") or "census_atp"
    if mode not in MODES:
        print("Unknown mode %r (choose %s)" % (mode, ", ".join(MODES)))
        return 2

    tz = _local_tz()
    windows = []
    if mode == "census_atp":
        if args.month:
            start, end = _month_bounds(args.month)
        elif args.start and args.end:
            start = dt.datetime.fromisoformat(args.start).replace(tzinfo=tz)
            end = dt.datetime.fromisoformat(args.end).replace(tzinfo=tz)
        else:
            ap.error("census mode needs --month, or both --from and --to")
        windows = [(start, end)]
    else:
        if not (args.quarter and args.week1 and args.week2):
            ap.error("sample modes need --quarter, --week1 and --week2")
        try:
            (w1s, w1e), (w2s, w2e) = validate_weeks(args.week1, args.week2,
                                                    args.quarter)
            start, end = quarter_bounds(args.quarter)
        except ValueError as exc:
            print("Refusing to run: %s" % exc)
            return 2
        windows = [(w1s, w1e), (w2s, w2e)]
    if mode == "sample_ath" and args.ath is None:
        print("Refusing to run: sample_ath needs --ath (aggregate tuning hours "
              "from your stream host).")
        return 2

    db_path = args.db
    if db_path is None:
        db_path = conf.get("paths", {}).get("db_path") or os.path.join(
            conf.get("paths", {}).get("data_dir", "data"), "studiofire.db")
    conn = coredb.connect(db_path)

    roots = list(cfg.get("share_aliases") or [])
    if not roots:
        nas = conf.get("paths", {}).get("nas_music_root")
        if nas:
            try:
                from .. import pathmap
                roots = [pathmap.share_root(nas)]
            except Exception:      # noqa: BLE001
                roots = [nas]
    drive = cfg.get("music_drive_letter") or ""

    index = build_precache_index(conn, roots, drive)
    print("index: %d digests from the library" % len(index))
    plays = []
    for w_start, w_end in windows:
        plays.extend(collect_period(conn, w_start, w_end, index=index))
    plays.sort(key=lambda r: r["start"])
    print("performances in %s: %d" % (
        " + ".join("%s..%s" % (a.date(), (b - dt.timedelta(days=1)).date())
                   for a, b in windows), len(plays)))
    if not plays:
        print("nothing to report for that period")
        return 2

    if not args.no_tags:
        try:
            from services import pathmap
            aliases = conf.get("paths", {}).get("path_aliases") or {}
            root = conf.get("paths", {}).get("nas_music_root")
            resolve = lambda p: pathmap.resolve(p, aliases, root)  # noqa: E731
        except Exception:          # noqa: BLE001
            resolve = None
        cache = os.path.join(os.path.dirname(db_path), "sx_tag_cache.json")
        print("tags: %s" % (enrich_tags(plays, cache_path=cache,
                                       resolve=resolve),))

    note = "no listener series supplied"
    if args.listeners:
        series = load_listener_series(args.listeners)
        missing = apply_listeners(plays, series)
        note = "%d listener samples; %d performances outside their coverage" % (
            len(series), missing)

    result = build_report(conn=conn, out_dir=args.out, cfg=cfg, mode=mode,
                          start=start, end_excl=end, plays=plays,
                          category=args.category, ath=args.ath,
                          headered=(args.fmt or
                                    cfg.get("output_format")) == "headered",
                          extra_note=note)
    print("files written to %s" % args.out)
    for name, path in result["files"].items():
        if name != "zip":
            print("  %-26s %s" % (name, os.path.basename(path)))
    print("  %-26s %s" % ("zip", os.path.basename(result["zip"])))
    print("rows in the filing: %d (from %d performances)"
          % (result["rows"], result["performances"]))
    print("due date: %s" % result["due"])
    if result["ready"]:
        print("READY TO FILE")
        return 0
    print("NOT READY — fix these first:")
    for i, reason in enumerate(result["reasons"], 1):
        print("  %d. %s" % (i, reason))
    return 2


if __name__ == "__main__":
    sys.exit(main())
