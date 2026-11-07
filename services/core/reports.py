"""Station reports — what aired, which playlist, and how often each spot ran.

Everything here is read-only SQL over `play_history` (+ the attribution tables the
feeder fills in). A report must never block the feeder: queries are bounded by a
date range, run on short-lived connections the caller passes in, and return
plain rows so the same data can be rendered on screen, exported as CSV, or
printed to PDF by the browser.

Two facts about the data shape matter and are why this module exists:

* `play_history.source` only says playlist/show/spot — it never said WHICH
  playlist or WHICH spot rule. Migration 15 added `queue_attribution` (written by
  the feeder, keyed by the precache path the journal reports back) plus
  `playlist_airings` (one row per stretch a playlist was on air).
* Nothing in the library knows genre/BPM/year — an artist-based "what kind of
  music" view is only possible once enrichment lands; this module does not
  pretend otherwise.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import time

from . import spots as spotmod

# --------------------------------------------------------------------- helpers


def _day_bounds(start: str, end: str) -> tuple[str, str]:
    """'YYYY-MM-DD' range, end-inclusive, as LOCAL ISO datetimes with offset.

    The offset matters: `play_history.ts` carries one, so a naive UTC compare
    would file an evening airing under the next day (and made the first version
    of the playlist report return nothing at all on a same-day run).
    """
    d0 = _dt.datetime.combine(_dt.date.fromisoformat(start), _dt.time.min)
    d1 = _dt.datetime.combine(_dt.date.fromisoformat(end), _dt.time.max)
    return d0.astimezone().isoformat(), d1.astimezone().isoformat()


def _epoch_bounds(start: str, end: str) -> tuple[float, float]:
    """Same range as epoch seconds (the attribution tables store epochs)."""
    d0 = _dt.datetime.combine(_dt.date.fromisoformat(start), _dt.time.min)
    d1 = _dt.datetime.combine(_dt.date.fromisoformat(end), _dt.time.max)
    return d0.timestamp(), d1.timestamp()


def _rows(conn: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _hours_between(a: float, b: float | None) -> float:
    end = b if b else _dt.datetime.now().timestamp()
    return max(0.0, (end - a) / 3600.0)


def _parse_ts(ts: str | None) -> _dt.datetime | None:
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts)
    except ValueError:
        return None


# ------------------------------------------------------------------- report 1

def playlist_airings(conn, start: str, end: str, playlist_id: int | None = None
                     ) -> dict:
    """Which playlist (or show) was on air, from when to when.

    Rows come from `playlist_airings` (opened/closed by the feeder). A stretch
    still running when the report is generated is reported as such rather than
    being closed with a made-up end time.
    """
    lo_ts, hi_ts = _epoch_bounds(start, end)
    args: list = [lo_ts, hi_ts]
    where = "WHERE pa.started_at BETWEEN ? AND ?"
    if playlist_id:
        where += " AND pa.playlist_id = ?"
        args.append(playlist_id)
    rows = _rows(conn, f"""
        SELECT pa.id, pa.playlist_id, pa.name, pa.started_at, pa.ended_at,
               pa.trigger, pa.ended_reason,
               (SELECT COUNT(*) FROM play_history ph
                 WHERE ph.event = 'track_start'
                   AND julianday(ph.ts) >= julianday(pa.started_at, 'unixepoch')
                   AND julianday(ph.ts) <= julianday(
                         COALESCE(pa.ended_at, strftime('%s','now')), 'unixepoch')
                   AND ph.title IS NOT NULL
                   AND (ph.source IN ('playlist','show')
                        OR json_extract(ph.extra, '$.attr_kind')
                           IN ('playlist','show'))
               ) AS tracks_aired
          FROM playlist_airings pa
          {where}
      ORDER BY pa.started_at DESC""", tuple(args))

    out = []
    for r in rows:
        start_dt = _dt.datetime.fromtimestamp(r["started_at"])
        end_dt = (_dt.datetime.fromtimestamp(r["ended_at"])
                  if r["ended_at"] else None)
        out.append({
            "playlist": r["name"] or (f"playlist #{r['playlist_id']}"
                                      if r["playlist_id"] else "—"),
            "started": start_dt.strftime("%Y-%m-%d %H:%M"),
            "ended": end_dt.strftime("%Y-%m-%d %H:%M") if end_dt else "still on air",
            "hours": round(_hours_between(r["started_at"], r["ended_at"]), 2),
            "tracks": r["tracks_aired"],
            "how": (r["trigger"] or "").title(),
            "ended_because": (r["ended_reason"] or "").replace("_", " "),
        })
    # ---- days reconstructed from attributed plays ------------------------
    # Migration 15 only starts recording airings from the day it installs. For
    # the period BEFORE that, songs still carry a playlist once they are
    # attributed (scripts/backfill_play_attribution.py), so for each DAY pick the
    # playlist that supplied most of the matched songs and report it as a stretch
    # covering that playlist's first and last play. A song can live in more than
    # one playlist, which is why this is per-day and marked estimated rather than
    # pretending to know each switch.
    lo, hi = _day_bounds(start, end)
    plays = _rows(conn, """
        SELECT ph.ts, json_extract(ph.extra, '$.playlist_id') AS pid,
               (SELECT name FROM playlists p
                 WHERE p.id = json_extract(ph.extra, '$.playlist_id')) AS name
          FROM play_history ph
         WHERE ph.event = 'track_start'
           AND json_extract(ph.extra, '$.playlist_id') IS NOT NULL
           AND julianday(ph.ts) BETWEEN julianday(?) AND julianday(?)
      ORDER BY ph.ts""", (lo, hi))

    from collections import defaultdict
    by_day: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    names: dict[int, str] = {}
    for p in plays:
        ts = _parse_ts(p["ts"])
        if ts is None:
            continue
        day = ts.date().isoformat()
        ep = ts.timestamp()
        by_day[day][p["pid"]].append(ep)
        if p["name"]:
            names[p["pid"]] = p["name"]

    recorded = [(r["started_at"], r["ended_at"] or time.time()) for r in rows]
    for day, per_pid in by_day.items():
        total = sum(len(v) for v in per_pid.values())
        if not total:
            continue
        # Only the day's DOMINANT playlist is reported as a stretch. Naming the
        # 3%-share lists would print a whole-day span for a playlist that only
        # supplied a handful of songs, which reads as a lie on paper.
        pid, eps = max(per_pid.items(), key=lambda kv: len(kv[1]))
        share = len(eps) / total
        if share < 0.4:
            continue
        first, last = min(eps), max(eps)
        if any(first >= a - 120 and last <= b + 120 for a, b in recorded):
            continue
        others = total - len(eps)
        out.append({
            "playlist": names.get(pid) or f"playlist #{pid}",
            "started": _dt.datetime.fromtimestamp(first).strftime(
                "%Y-%m-%d %H:%M"),
            "ended": _dt.datetime.fromtimestamp(last).strftime(
                "%Y-%m-%d %H:%M"),
            "hours": round(_hours_between(first, last), 2),
            "tracks": len(eps),
            "how": "Estimated from plays",
            "ended_because": f"{int(round(share * 100))}% of the day" +
                             (f" ({others} songs from other lists)" if others else ""),
        })
    out.sort(key=lambda r: r["started"], reverse=True)

    return {
        "id": "playlist-airings",
        "title": "Playlist airings — what played, from when to when",
        "subtitle": f"{start} to {end}",
        "columns": ["playlist", "started", "ended", "hours", "tracks", "how",
                    "ended_because"],
        "headers": ["Playlist", "From", "To", "Hours", "Tracks", "Started by",
                    "Ended because"],
        "rows": out,
        "coverage": _attribution_coverage(conn, lo, hi),
        "note": ("“Estimated from plays” rows are reconstructed from the songs "
                 "that aired before airing-stretch recording was installed: the "
                 "start is the first song, the end the last, and a gap longer "
                 "than 20 minutes splits the stretch. They are close, not "
                 "exact — treat recorded rows as the fact and estimates as the "
                 "best reconstruction of older history."),
    }


# ------------------------------------------------------------------- report 2

def _window_days(rule: dict, start: str, end: str) -> list[_dt.date]:
    """Days in the report window that the rule's own run window also covers."""
    d0 = _dt.date.fromisoformat(start)
    d1 = _dt.date.fromisoformat(end)
    sd, ed = rule.get("start_date"), rule.get("end_date")
    out = []
    day = d0
    while day <= d1:
        iso = day.isoformat()
        if not (sd and iso < sd) and not (ed and iso > ed):
            out.append(day)
        day += _dt.timedelta(days=1)
    return out


def _expected_airings(rule: dict, start: str, end: str) -> int:
    """How many times the rule SHOULD have fired in the window.

    Computed from the rule's own trigger, not from what aired — that is the whole
    point: a spot scheduled every 30 minutes owes 48 airings a day, and the
    report shows when it delivered fewer.
    """
    if not rule.get("enabled"):
        return 0
    trig = rule["trigger"]
    if trig == "manual":
        return 0
    days = _window_days(rule, start, end)
    if not days:
        return 0
    if trig == "once":
        sa = (rule.get("start_at") or "")[:10]
        return sum(1 for d in days if d.isoformat() == sa)
    if trig == "daily":
        return len(days)
    if trig == "weekly":
        mask = rule.get("days_mask") or 0
        return sum(1 for d in days if mask & (1 << d.weekday()))
    if trig == "interval":
        every = max(1, int(rule.get("interval_min") or 60))
        return len(days) * (1440 // every)
    if trig == "clock":
        marks = spotmod._parse_minutes(rule.get("clock_minutes")) or []
        return len(days) * 24 * max(1, len(marks))
    return 0


def spot_airings(conn, start: str, end: str, include_deleted: bool = False
                 ) -> dict:
    """Per spot rule: how many times it actually aired, vs how many it should
    have. This is the report John needs — it shows the spot that is NOT airing.

    Actual counts use the attribution the feeder now records (spot_rule_id) and
    fall back to the title prefix for airings from before attribution existed.
    """
    lo, hi = _day_bounds(start, end)
    rules = [dict(r) for r in conn.execute(
        "SELECT * FROM spot_rules " +
        ("" if include_deleted else "WHERE deleted_at IS NULL ") +
        "ORDER BY label").fetchall()]

    out = []
    for r in rules:
        by_id = conn.execute(
            "SELECT COUNT(*) FROM play_history "
            "WHERE event='track_start' AND source='spot' "
            "AND julianday(ts) BETWEEN julianday(?) AND julianday(?) "
            "AND json_extract(extra, '$.spot_rule_id') = ?",
            (lo, hi, r["id"])).fetchone()[0]
        # historical fallback: titles are written as "<label>: <file>"
        by_label = conn.execute(
            "SELECT COUNT(*) FROM play_history "
            "WHERE event='track_start' AND source='spot' "
            "AND julianday(ts) BETWEEN julianday(?) AND julianday(?) "
            "AND title LIKE ? AND json_extract(extra, '$.spot_rule_id') IS NULL",
            (lo, hi, (r["label"] or "") + "%")).fetchone()[0]
        actual = by_id + by_label
        last = conn.execute(
            "SELECT ts FROM play_history WHERE event='track_start' "
            "AND source='spot' AND julianday(ts) BETWEEN julianday(?) AND julianday(?) "
            "AND (json_extract(extra, '$.spot_rule_id') = ? OR "
            "     (json_extract(extra, '$.spot_rule_id') IS NULL AND title LIKE ?)) "
            "ORDER BY ts DESC LIMIT 1",
            (lo, hi, r["id"], (r["label"] or "") + "%")).fetchone()
        expected = _expected_airings(r, start, end)
        gap = expected - actual
        out.append({
            "spot": r["label"],
            "kind": {"ids": "Legal ID", "ads": "Underwriting"}.get(
                spotmod.rule_kind(conn, r) or "", "—"),
            "schedule": spotmod.describe(r),
            "expected": expected,
            "aired": actual,
            "missed": gap if gap > 0 else 0,
            "status": ("OK" if expected and actual >= expected
                       else "UNDER" if actual else "NOT AIRED"),
            "last_aired": (last["ts"][:16].replace("T", " ") if last else "never"),
            "enabled": "yes" if r["enabled"] else "paused",
        })
    return {
        "id": "spot-airings",
        "title": "Spots — did each one actually air?",
        "subtitle": f"{start} to {end}",
        "columns": ["spot", "kind", "schedule", "expected", "aired", "missed",
                    "status", "last_aired", "enabled"],
        "headers": ["Spot", "Type", "Schedule", "Should have aired", "Aired",
                    "Short by", "Status", "Last aired", "Enabled"],
        "rows": out,
        "note": ("Aired counts come from the as-aired log. Counts before this "
                 "version are matched on the spot's name, so a renamed rule can "
                 "read low for that older period."),
    }


# ------------------------------------------------------------------- report 3

def as_aired(conn, start: str, end: str, kind: str = "all") -> dict:
    """The classic as-aired log: every track, ID, ad and PSA in order."""
    lo, hi = _day_bounds(start, end)
    where = "WHERE event='track_start' AND julianday(ts) BETWEEN julianday(?) AND julianday(?)"
    args: list = [lo, hi]
    if kind == "music":
        where += " AND source IN ('playlist','show','manual')"
    elif kind == "spots":
        where += " AND source = 'spot'"
    rows = _rows(conn, f"""
        SELECT ts, title, source,
               json_extract(extra, '$.attr_kind')    AS attr_kind,
               json_extract(extra, '$.playlist_id')  AS playlist_id
          FROM play_history {where} ORDER BY ts""", tuple(args))
    out = []
    for r in rows:
        src = r["attr_kind"] or r["source"] or "unknown"
        out.append({
            "when": (r["ts"] or "")[:19].replace("T", " "),
            "what": r["title"] or "",
            "type": {"spot": "spot", "playlist": "playlist", "show": "show",
                     "emergency": "EMERGENCY FILLER", "manual": "manual"}.get(
                         src, src),
        })
    return {
        "id": "as-aired",
        "title": "As aired — everything that went out",
        "subtitle": f"{start} to {end}",
        "columns": ["when", "what", "type"],
        "headers": ["When", "What aired", "Type"],
        "rows": out,
        "note": "Proof of performance: one line per track, ID, ad or PSA.",
    }


# ------------------------------------------------------------------- report 4

def legal_id_hours(conn, start: str, end: str) -> dict:
    """Every hour in the window, and whether a legal station ID aired in it.

    47 CFR 73.1201 requires the ID near the top of each hour; a station manager
    should be able to see the gaps without grepping. Hours with no legal-ID rule
    configured are reported as "no rule", not as a pass.
    """
    lo, hi = _day_bounds(start, end)
    id_rules = spotmod.legal_id_rules(conn)
    counts: dict[str, int] = {}

    ids = [r["id"] for r in id_rules]
    if ids:
        marks = ",".join("?" for _ in ids)
        for r in conn.execute(
                "SELECT substr(ts, 1, 13) AS hour, COUNT(*) AS n "
                "FROM play_history WHERE event='track_start' "
                "AND source='spot' "
                "AND julianday(ts) BETWEEN julianday(?) AND julianday(?) "
                "AND json_extract(extra, '$.spot_rule_id') IN (" + marks + ") "
                "GROUP BY hour", (lo, hi, *ids)).fetchall():
            counts[r[0]] = counts.get(r[0], 0) + r[1]
        # airings from before attribution existed: match on the rule's name
        for rule in id_rules:
            for r in conn.execute(
                    "SELECT substr(ts, 1, 13) AS hour, COUNT(*) AS n "
                    "FROM play_history WHERE event='track_start' "
                    "AND source='spot' AND title LIKE ? "
                    "AND julianday(ts) BETWEEN julianday(?) AND julianday(?) "
                    "AND json_extract(extra, '$.spot_rule_id') IS NULL "
                    "GROUP BY hour",
                    ((rule.get("label") or "") + "%", lo, hi)).fetchall():
                counts[r[0]] = counts.get(r[0], 0) + r[1]

    out = []
    day = _dt.date.fromisoformat(start)
    last_day = _dt.date.fromisoformat(end)
    while day <= last_day:
        for h in range(24):
            hour = "%sT%02d" % (day.isoformat(), h)
            n = counts.get(hour, 0)
            out.append({
                "hour": "%s %02d:00" % (day.isoformat(), h),
                "ids": n,
                "status": ("OK" if n else
                           ("NO RULE" if not id_rules else "MISSED")),
            })
        day += _dt.timedelta(days=1)
    return {
        "id": "legal-id-hours",
        "title": "Legal ID - did every hour have one?",
        "subtitle": "%s to %s" % (start, end),
        "columns": ["hour", "ids", "status"],
        "headers": ["Hour", "Legal IDs heard", "Status"],
        "rows": out,
        "note": ("47 CFR 73.1201 wants the station ID near the top of each hour. "
                 "NO RULE means no legal-ID spot rule is enabled - a policy "
                 "problem, not a quiet hour."),
    }


# ------------------------------------------------------------------- report 5

def spot_detail(conn, start: str, end: str) -> dict:
    """Every spot airing, in order - the affidavit John hands an underwriter."""
    lo, hi = _day_bounds(start, end)
    rows = _rows(conn, """
        SELECT ph.ts, ph.title,
               json_extract(ph.extra, '$.spot_rule_id') AS rid,
               json_extract(ph.extra, '$.attr_label')   AS label
          FROM play_history ph
         WHERE ph.event = 'track_start' AND ph.source = 'spot'
           AND julianday(ph.ts) BETWEEN julianday(?) AND julianday(?)
      ORDER BY ph.ts""", (lo, hi))
    kinds = {r["id"]: (spotmod.rule_kind(conn, r) or "")
             for r in spotmod.list_all(conn)}
    out = []
    for r in rows:
        title = r["title"] or ""
        folder, _, item = title.partition(": ")
        kind = kinds.get(r["rid"] or -1, "")
        out.append({
            "when": (r["ts"] or "")[:19].replace("T", " "),
            "spot": folder or r["label"] or "",
            "item": item or "",
            "type": {"ids": "Legal ID", "ads": "Underwriting"}.get(
                kind, "PSA / promo"),
        })
    return {
        "id": "spot-detail",
        "title": "Every spot airing - affidavit detail",
        "subtitle": "%s to %s" % (start, end),
        "columns": ["when", "spot", "item", "type"],
        "headers": ["Aired", "Spot", "Item", "Type"],
        "rows": out,
        "note": ("One line per spot that actually aired, in order. Pair it with "
                 "the Spots report (what was scheduled) for a complete proof of "
                 "performance."),
    }


# ------------------------------------------------------------------- report 6

def play_census(conn, start: str, end: str) -> dict:
    """Spins per track - the shape a SoundExchange census wants, INTERNAL only."""
    lo, hi = _day_bounds(start, end)
    rows = _rows(conn, """
        SELECT ph.title, COUNT(*) AS spins, MIN(ph.ts) AS first_ts,
               MAX(ph.ts) AS last_ts
          FROM play_history ph
         WHERE ph.event = 'track_start'
           AND ph.source = 'playlist'
           AND julianday(ph.ts) BETWEEN julianday(?) AND julianday(?)
      GROUP BY ph.title ORDER BY spins DESC""", (lo, hi))
    out = [{
        "track": r["title"] or "",
        "spins": r["spins"],
        "first": (r["first_ts"] or "")[:16].replace("T", " "),
        "last": (r["last_ts"] or "")[:16].replace("T", " "),
    } for r in rows]
    return {
        "id": "play-census",
        "title": "Play census - spins per track (internal)",
        "subtitle": "%s to %s" % (start, end),
        "columns": ["track", "spins", "first", "last"],
        "headers": ["Track", "Spins", "First play", "Last play"],
        "rows": out,
        "note": ("INTERNAL census, not a SoundExchange filing: StudioFire holds "
                 "no ISRC, marketing label or audience counts, which a Report of "
                 "Use requires. Use it to check rotation and repeats."),
    }


# ------------------------------------------------------------------- report 7

def exceptions(conn, start: str, end: str) -> dict:
    """What went wrong: songs skipped as missing, tracks cut short, library gaps.

    Cheap and honest — every row here is a real event already recorded, not an
    inference. This is the report that explains a strange as-aired log.
    """
    lo_ts, hi_ts = _epoch_bounds(start, end)
    lo, hi = _day_bounds(start, end)
    out = []

    # one row per FILE, not per skip: a playlist laps, so the same dead file is
    # re-skipped every pass and an event list is unreadable within a day
    for r in _rows(conn, """
            SELECT path, MAX(ts) AS last_ts, COUNT(*) AS n
              FROM skipped_items WHERE ts BETWEEN ? AND ?
          GROUP BY path ORDER BY last_ts DESC""", (lo_ts, hi_ts)):
        name = (r["path"] or "").replace("/", "\\").split("\\")[-1]
        out.append({
            "when": _dt.datetime.fromtimestamp(r["last_ts"]).strftime(
                "%Y-%m-%d %H:%M"),
            "kind": "Skipped (file missing)",
            "detail": "%s%s" % (name, (" (x%d)" % r["n"]) if r["n"] > 1 else ""),
        })

    for r in _rows(conn, """
            SELECT ph.ts, ph.title,
                   json_extract(ph.extra, '$.reason') AS reason
              FROM play_history ph
             WHERE ph.event = 'track_skip'
               AND julianday(ph.ts) BETWEEN julianday(?) AND julianday(?)
          ORDER BY ph.ts DESC""", (lo, hi)):
        out.append({
            "when": (r["ts"] or "")[:16].replace("T", " "),
            "kind": "Track skipped by the engine",
            "detail": (r["title"] or "") + (
                (" (" + str(r["reason"]) + ")") if r["reason"] else ""),
        })

    missing = conn.execute("SELECT COUNT(*) FROM tracks WHERE missing = 1"
                           ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    if missing:
        out.append({
            "when": "now",
            "kind": "Library files marked missing",
            "detail": "%d of %d indexed files can no longer be found" % (
                missing, total),
        })

    out.sort(key=lambda r: r["when"], reverse=True)
    return {
        "id": "exceptions",
        "title": "Exceptions - what went wrong",
        "subtitle": "%s to %s" % (start, end),
        "columns": ["when", "kind", "detail"],
        "headers": ["When", "What happened", "Detail"],
        "rows": out,
        "note": ("Every row is an event StudioFire actually recorded: a song it "
                 "had to skip, a track cut short, or a library file that has "
                 "gone missing. An empty report means nothing went wrong."),
    }


def _attribution_coverage(conn, lo: str, hi: str) -> dict:
    """How much of the window's music we can actually attribute to a playlist.

    Reports must disclose this: a named-playlist view built on a third of the
    plays is evidence with a hole in it, and the hole belongs on the page.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN json_extract(extra, '$.playlist_id') IS NOT NULL "
        "                THEN 1 ELSE 0 END) AS matched "
        "  FROM play_history WHERE event='track_start' "
        "   AND source IN ('playlist','show','manual') "
        "   AND julianday(ts) BETWEEN julianday(?) AND julianday(?)",
        (lo, hi)).fetchone()
    total = row["total"] if row else 0
    matched = row["matched"] if row else 0
    return {"total": total, "matched": matched or 0,
            "label": "could be tied to a playlist",
            "pct": round(100.0 * (matched or 0) / total, 1) if total else 0.0}



# Folders that hold station furniture, not music. Anything played out of these
# is a spot, PSA, liner or legal ID and has no business in an artist/genre
# count, even when it is scheduled from inside a playlist.
FURNITURE_SETTINGS = ("dir_ads", "dir_psas", "dir_jingles", "dir_station_ids")


def _furniture_clause(conn) -> tuple[str, list]:
    """SQL fragment + params excluding non-music folders (set in Settings)."""
    pats: list[str] = []
    seen = set()
    for key in FURNITURE_SETTINGS:
        row = conn.execute("SELECT value FROM settings WHERE key = ?",
                           (key,)).fetchone()
        if not row or not row[0]:
            continue
        for part in str(row[0]).split(";"):
            p = part.strip().rstrip("\\/")
            if not p:
                continue
            norm = p.lower().replace("/", "\\") + "\\%"
            if norm not in seen:
                seen.add(norm)
                pats.append(norm)
    for (fp,) in conn.execute(
            "SELECT folder_path FROM spot_rules "
            " WHERE folder_path IS NOT NULL AND folder_path <> ''"):
        p = str(fp).strip().rstrip("\\/")
        if not p:
            continue
        norm = p.lower().replace("/", "\\") + "\\%"
        if norm not in seen:
            seen.add(norm)
            pats.append(norm)
    if not pats:
        return "", []
    path_expr = "lower(replace(pi.path, '/', char(92)))"
    clause = ("           AND NOT (" +
              " OR ".join("%s LIKE ?" % path_expr for _ in pats) + ")\n")
    return clause, pats


# --------------------------------------------------------------- charts (SVG)

def bars_svg(series, width=760, bar_h=22, gap=6, unit="plays",
             colour="#3f5be0"):
    """A horizontal bar chart as inline SVG.

    Why SVG and not a plotting library: the station updater ships SOURCE ONLY, so
    matplotlib/pandas are not available on a box that installs from a release.
    Inline SVG needs nothing, prints crisply at any size, and the browser's own
    Print -> Save as PDF renders it into the delivered document.
    """
    series = [(str(k), float(v)) for k, v in series if v]
    if not series:
        return ""
    top = max(v for _, v in series) or 1
    label_w, value_w = 190, 62
    height = len(series) * (bar_h + gap) + 10
    plot_w = max(120, width - label_w - value_w - 8)
    parts = ['<svg class="chart" viewBox="0 0 %d %d" width="100%%" '
             'height="%d" role="img" xmlns="http://www.w3.org/2000/svg">'
             % (width, height, height)]
    for i, (label, value) in enumerate(series):
        y = i * (bar_h + gap) + 4
        w = max(2.0, plot_w * (value / top))
        short = label if len(label) <= 30 else label[:29] + "…"
        # currentColor, so the same SVG is legible on the dark screen and on
        # the white printed page (the CSS sets the colour per context)
        parts.append(
            '<text x="0" y="%d" font-size="11.5" fill="currentColor">%s</text>'
            % (y + bar_h * 0.72, _xml(short)))
        parts.append(
            '<rect x="%d" y="%d" width="%.1f" height="%d" rx="4" '
            'fill="%s"></rect>' % (label_w, y, w, bar_h, colour))
        parts.append(
            '<text x="%.1f" y="%d" font-size="11.5" fill="currentColor" '
            'opacity="0.65">%s %s</text>'
            % (label_w + w + 6, y + bar_h * 0.72,
               ("%d" % value) if value == int(value) else ("%.1f" % value),
               unit))
    parts.append("</svg>")
    return "".join(parts)


def _xml(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _music_join(conn, lo: str, hi: str, group_col: str, limit: int | None = None):
    """Plays grouped by a TRACK column (genre / artist).

    Only plays we can tie to a real library row are counted — the join goes
    through the attribution the feeder writes (playlist item -> track path). The
    caller discloses the coverage so nobody reads a subset as the whole period.
    """
    furniture_sql, furniture_params = _furniture_clause(conn)
    sql = """
        SELECT COALESCE(NULLIF(TRIM(t.%s), ''), 'Unknown') AS grp,
               COUNT(*) AS spins
          FROM play_history ph
          JOIN playlist_items pi
            ON pi.id = json_extract(ph.extra, '$.item_id')
          -- Two things make a naive equality join lie here:
          --  * the index and the playlist rows disagree on the separator
          --    (forward vs back slashes), so paths are normalised; and
          --  * the same file can appear in `tracks` more than once (one row per
          --    scan vintage), and the older twin is often the one with no
          --    artist/genre, which is how an artist report of almost all
          --    "Unknown" happened on a library where most rows HAD an artist.
          -- So pick ONE row per file, preferring tagged, present, enriched.
          JOIN tracks t ON t.id = (
               SELECT t2.id FROM tracks t2
                WHERE lower(replace(t2.path, '/', char(92)))
                    = lower(replace(pi.path, '/', char(92)))
                ORDER BY (t2.missing = 0) DESC,
                         (t2.artist IS NOT NULL
                          AND TRIM(t2.artist) <> '') DESC,
                         (t2.genre IS NOT NULL) DESC,
                         t2.meta_read DESC, t2.id
                LIMIT 1)
         WHERE ph.event = 'track_start'
           AND ph.source = 'playlist'
           %s           AND COALESCE(TRIM(t.%s), '') <> ''
           AND julianday(ph.ts) BETWEEN julianday(?) AND julianday(?)
      GROUP BY grp
      ORDER BY spins DESC""" % (group_col, furniture_sql, group_col)
    args = furniture_params + [lo, hi]
    if limit:
        sql += " LIMIT %d" % int(limit)
    return _rows(conn, sql, tuple(args))


def _music_coverage(conn, lo: str, hi: str, column: str) -> dict:
    """How many music plays carried a usable tag for the column we report on.

    Mark's rule: an artist report is only worth printing where the artist is
    actually known, so plays with no tag are counted here and left out of the
    rows — never shown as a lump of "Unknown" that hides the real coverage.
    """
    furniture_sql, furniture_params = _furniture_clause(conn)
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN COALESCE(TRIM(t." + column + "), '') <> '' "
        "                THEN 1 ELSE 0 END) AS matched "
        "  FROM play_history ph "
        "  JOIN playlist_items pi "
        "    ON pi.id = json_extract(ph.extra, '$.item_id') "
        "  LEFT JOIN tracks t ON t.id = ("
        "       SELECT t2.id FROM tracks t2"
        "        WHERE lower(replace(t2.path, '/', char(92)))"
        "            = lower(replace(pi.path, '/', char(92)))"
        "        ORDER BY (t2.missing = 0) DESC,"
        "                 (COALESCE(TRIM(t2." + column + "), '') <> '') DESC,"
        "                 t2.meta_read DESC, t2.id LIMIT 1) "
        " WHERE ph.event = 'track_start' "
        "   AND ph.source = 'playlist' "
        + furniture_sql +
        "   AND julianday(ph.ts) BETWEEN julianday(?) AND julianday(?)",
        tuple(furniture_params) + (lo, hi)).fetchone()
    total = row["total"] if row else 0
    matched = (row["matched"] if row else 0) or 0
    return {"total": total, "matched": matched,
            "label": ("had an artist tag" if column == "artist"
                      else "had a genre tag"),
            "pct": round(100.0 * matched / total, 1) if total else 0.0}


# ------------------------------------------------------------------- report 8

def genre_mix(conn, start: str, end: str) -> dict:
    """What kind of music actually played — by plays and by distinct tracks.

    Possible only because the library files carry genre tags and the indexer now
    reads them (migration 16). A track with no genre tag is counted as "Unknown"
    rather than being guessed from its folder name.
    """
    lo, hi = _day_bounds(start, end)
    plays = _music_join(conn, lo, hi, "genre")
    cov = _music_coverage(conn, lo, hi, "genre")
    lib = conn.execute(
        "SELECT genre, COUNT(*) n FROM tracks WHERE missing = 0 "
        "GROUP BY genre ORDER BY n DESC").fetchall()
    # untagged files are reported separately, not as a giant "None" bar that
    # hides the real shape of the library
    untagged = sum(r[1] for r in lib if not r[0])
    lib_rows = [(r[0], r[1]) for r in lib if r[0]][:12]
    series = [(r["grp"], r["spins"]) for r in plays]
    total_plays = sum(v for _, v in series)
    rows = [{"genre": g, "plays": n,
             "share": round(100.0 * n / total_plays, 1) if total_plays else 0.0}
            for g, n in series[:40]]
    charts = []
    if series:
        charts.append({"title": "Plays by genre",
                       "svg": bars_svg(series[:12])})
    if lib_rows:
        charts.append({"title": "Library size by genre (tracks indexed%s)"
                                % (", %d files not tagged yet" % untagged
                                   if untagged else ""),
                       "svg": bars_svg(lib_rows, unit="tracks",
                                       colour="#f26a1b")})
    return {
        "id": "genre-mix",
        "title": "What kind of music played — by genre",
        "subtitle": "%s to %s" % (start, end),
        "columns": ["genre", "plays", "share"],
        "headers": ["Genre", "Plays", "% of plays"],
        "rows": rows,
        "charts": charts,
        "coverage": cov,
        "note": ("Playlist airings only — shows are excluded, since a show's "
                 "mp3 does not carry a per-song artist we can trust. Genre "
                 "comes from each file's own tag, read offline and never "
                 "guessed from a folder name; a song with no genre tag is "
                 "counted in the coverage line, not printed as 'Unknown'."),
    }


# ------------------------------------------------------------------- report 9

def artist_spins(conn, start: str, end: str) -> dict:
    """How many times each artist aired (spins, not songs)."""
    lo, hi = _day_bounds(start, end)
    plays = _music_join(conn, lo, hi, "artist")
    cov = _music_coverage(conn, lo, hi, "artist")
    series = [(r["grp"], r["spins"]) for r in plays]
    total = sum(v for _, v in series)
    rows = [{"artist": a, "spins": n,
             "share": round(100.0 * n / total, 1) if total else 0.0}
            for a, n in series[:60]]
    charts = ([{"title": "Most-aired artists",
                "svg": bars_svg(series[:15], unit="spins")}]
              if series else [])
    return {
        "id": "artist-spins",
        "title": "How many times each artist aired",
        "subtitle": "%s to %s" % (start, end),
        "columns": ["artist", "spins", "share"],
        "headers": ["Artist", "Spins", "% of plays"],
        "rows": rows,
        "charts": charts,
        "coverage": cov,
        "note": ("Playlist airings only — shows are excluded, because a show "
                 "mp3 does not carry a per-song artist we can trust. A spin is "
                 "one play of one song; an artist with three songs in rotation "
                 "can out-count one with a single hit. Songs with no artist tag "
                 "are counted in the coverage line, not printed as 'Unknown'."),
    }


# --------------------------------------------------------------------- registry

CATALOG = [
    {"id": "as-aired", "name": "As aired — everything",
     "blurb": "Every track, legal ID, ad and PSA in order. The proof-of-performance log."},
    {"id": "playlist-airings", "name": "Playlist airings — what played, when",
     "blurb": "Which playlist or show was on air, from when to when, and how many songs it aired."},
    {"id": "spot-airings", "name": "Spots — did each one air?",
     "blurb": "Per spot: how many times it should have run, how many it did, and what was missed."},
    {"id": "spot-detail", "name": "Every spot airing — affidavit detail",
     "blurb": "One line per spot that aired, in order. The proof an underwriter asks for."},
    {"id": "legal-id-hours", "name": "Legal ID — did every hour have one?",
     "blurb": "Hour by hour: was a station ID heard? Flags missed hours and a missing rule."},
    {"id": "play-census", "name": "Play census — spins per track (internal)",
     "blurb": "How often each track ran. Internal only — not a SoundExchange filing."},
    {"id": "exceptions", "name": "Exceptions — what went wrong",
     "blurb": "Songs skipped as missing, tracks cut short, library files gone. Quiet days = empty report."},
    {"id": "genre-mix", "name": "Music mix — plays by genre (charts)",
     "blurb": "What kind of music aired, from each file's own genre tag, with a chart. Best printed."},
    {"id": "artist-spins", "name": "Artist airplay — spins per artist (charts)",
     "blurb": "How many times each artist aired in the period, with a chart."},
]

_RUNNERS = {
    "as-aired": as_aired,
    "playlist-airings": playlist_airings,
    "spot-airings": spot_airings,
    "spot-detail": spot_detail,
    "legal-id-hours": legal_id_hours,
    "play-census": play_census,
    "exceptions": exceptions,
    "genre-mix": genre_mix,
    "artist-spins": artist_spins,
}


def catalog() -> list[dict]:
    return CATALOG


def run(report_id: str, conn, start: str, end: str, **kw) -> dict:
    fn = _RUNNERS.get(report_id)
    if fn is None:
        raise ValueError("unknown report")
    return fn(conn, start, end, **kw)


def to_csv(report: dict) -> str:
    """Excel-safe CSV: quoted, UTF-8 with a BOM so accented artist names open
    correctly in Excel on a Windows office machine."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    w.writerow([report.get("title", "")])
    w.writerow([report.get("subtitle", "")])
    w.writerow([])
    w.writerow(report["headers"])
    for r in report["rows"]:
        w.writerow([r.get(c, "") for c in report["columns"]])
    if report.get("note"):
        w.writerow([])
        w.writerow([report["note"]])
    return "\ufeff" + buf.getvalue()
