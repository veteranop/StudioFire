"""P2 database layer — SQLite in WAL mode, versioned schema (PLAN.md §5).

P1 NEVER touches this database (design law). Migrations run at startup;
schema_version is tracked in-DB so updates can migrate old deployments
automatically (PLAN.md Phase 4 depends on this existing from day one).

Connections are cheap and short-lived: one per request/job via connect().
WAL mode makes concurrent readers + one writer safe across P2/P3/P4.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time

log = logging.getLogger("core.db")

# §6.4: how many pre-migration DB snapshots to keep (the last N).
BACKUP_KEEP = 5

# Each entry: (version, sql script). Append-only — never edit a shipped one.
MIGRATIONS: list[tuple[int, str]] = [
    (1, """
CREATE TABLE users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'operator')),
    created_at    REAL NOT NULL
);

CREATE TABLE tracks (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    duration_sec REAL,
    format       TEXT,
    size         INTEGER,
    mtime        REAL,
    indexed_at   REAL NOT NULL,
    missing      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_tracks_artist ON tracks(artist);
CREATE INDEX idx_tracks_title  ON tracks(title);

CREATE TABLE playlists (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE playlist_items (
    id          INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    item_type   TEXT NOT NULL
        CHECK (item_type IN ('file', 'folder-newest', 'folder-rotation')),
    path        TEXT NOT NULL,   -- file path or folder path per item_type
    title       TEXT             -- display label (track title or folder label)
);
CREATE INDEX idx_items_playlist ON playlist_items(playlist_id, position);

-- persisted round-robin cursor per folder (PLAN.md §10.5: survives restarts)
CREATE TABLE rotation_state (
    folder_path TEXT PRIMARY KEY,
    next_index  INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL
);

-- P1 journal ingested here (PLAN.md §10.4); journal_id dedupes re-ingestion
CREATE TABLE play_history (
    id         INTEGER PRIMARY KEY,
    journal_id TEXT NOT NULL UNIQUE,
    ts         TEXT NOT NULL,
    event      TEXT NOT NULL,
    path       TEXT,
    title      TEXT,
    source     TEXT,
    extra      TEXT
);
CREATE INDEX idx_history_ts ON play_history(ts);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""),
    (2, """
-- Scheduled/cued playlists ("shows") that interrupt the base rotation once,
-- then hand back to it (PLAN.md §6 Phase 3 scheduling, MVP). start_at is a
-- local 'YYYY-MM-DDTHH:MM' timestamp, or NULL for a manual (button) cue.
CREATE TABLE playlist_schedule (
    id          INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    start_at    TEXT,
    sort        INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'waiting'
        CHECK (state IN ('waiting', 'playing', 'done')),
    created_at  REAL NOT NULL
);
CREATE INDEX idx_sched_state ON playlist_schedule(state, start_at);
"""),
    (3, """
-- Spot rules: Station IDs / ads / jingles / PSAs pulled from a settings
-- folder (folder_key) and inserted at the next song boundary. One folder +
-- one trigger per rule; the file is chosen round-robin (rotation_state).
--   trigger: 'interval' (interval_min), 'clock' (clock_minutes CSV of
--            minute-of-hour), 'once' (start_at local), or 'manual' (button).
CREATE TABLE spot_rules (
    id            INTEGER PRIMARY KEY,
    folder_key    TEXT NOT NULL,
    label         TEXT,
    trigger       TEXT NOT NULL
        CHECK (trigger IN ('interval', 'clock', 'once', 'manual')),
    interval_min  INTEGER,
    clock_minutes TEXT,
    start_at      TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_fired    REAL,
    created_at    REAL NOT NULL
);
"""),
    (4, """
-- Scheduleable sources beyond whole playlists/folders: a single audio file
-- (e.g. a whole show as one .mp3) or a ZaraRadio .lst read live.
-- Spots can now target one specific file instead of a folder.
ALTER TABLE spot_rules ADD COLUMN file_path TEXT;

-- Rebuild playlist_schedule so playlist_id is nullable and it carries a typed
-- source (playlist / file / lst). SQLite can't relax a NOT NULL column in place.
CREATE TABLE playlist_schedule_new (
    id          INTEGER PRIMARY KEY,
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL DEFAULT 'playlist'
        CHECK (source_kind IN ('playlist', 'file', 'lst')),
    source_path TEXT,
    start_at    TEXT,
    sort        INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'waiting'
        CHECK (state IN ('waiting', 'playing', 'done')),
    created_at  REAL NOT NULL
);
INSERT INTO playlist_schedule_new
    (id, playlist_id, source_kind, source_path, start_at, sort, state, created_at)
    SELECT id, playlist_id, 'playlist', NULL, start_at, sort, state, created_at
    FROM playlist_schedule;
DROP TABLE playlist_schedule;
ALTER TABLE playlist_schedule_new RENAME TO playlist_schedule;
CREATE INDEX idx_sched_state ON playlist_schedule(state, start_at);
"""),
    (5, """
-- Recurring shows + a run window. A scheduled show can repeat every day or on
-- chosen weekdays at a time-of-day, and stop after an end date (e.g. an event
-- promo that runs 'until August'). 'once' rows keep using start_at unchanged.
--   recurrence : 'once' (start_at) | 'daily' | 'weekly'
--   time_of_day: 'HH:MM' local, for daily/weekly
--   days_mask  : weekly weekday bitmask, bit0=Mon .. bit6=Sun
--   start_date : 'YYYY-MM-DD' inclusive earliest air date (NULL = right away)
--   end_date   : 'YYYY-MM-DD' inclusive last air date   (NULL = forever)
--   last_fired : 'YYYY-MM-DD' of the last auto-fire, so a slot fires once a day
-- (validated in code; SQLite ADD COLUMN can't safely carry cross-checks).
ALTER TABLE playlist_schedule ADD COLUMN recurrence  TEXT NOT NULL DEFAULT 'once';
ALTER TABLE playlist_schedule ADD COLUMN time_of_day TEXT;
ALTER TABLE playlist_schedule ADD COLUMN days_mask   INTEGER;
ALTER TABLE playlist_schedule ADD COLUMN start_date  TEXT;
ALTER TABLE playlist_schedule ADD COLUMN end_date    TEXT;
ALTER TABLE playlist_schedule ADD COLUMN last_fired  TEXT;
"""),
    (6, """
-- 'folder' as a schedulable show source: a folder of segments (e.g. Floydian
-- Slip) played in filename order, always whatever files are in it at air time.
-- Rebuild to widen the source_kind CHECK (SQLite can't alter a CHECK in place).
CREATE TABLE playlist_schedule_new (
    id          INTEGER PRIMARY KEY,
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL DEFAULT 'playlist'
        CHECK (source_kind IN ('playlist', 'file', 'lst', 'folder')),
    source_path TEXT,
    start_at    TEXT,
    sort        INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'waiting'
        CHECK (state IN ('waiting', 'playing', 'done')),
    created_at  REAL NOT NULL,
    recurrence  TEXT NOT NULL DEFAULT 'once',
    time_of_day TEXT,
    days_mask   INTEGER,
    start_date  TEXT,
    end_date    TEXT,
    last_fired  TEXT
);
INSERT INTO playlist_schedule_new SELECT
    id, playlist_id, source_kind, source_path, start_at, sort, state,
    created_at, recurrence, time_of_day, days_mask, start_date, end_date,
    last_fired FROM playlist_schedule;
DROP TABLE playlist_schedule;
ALTER TABLE playlist_schedule_new RENAME TO playlist_schedule;
CREATE INDEX idx_sched_state ON playlist_schedule(state, start_at);

-- Spots get the same daily/weekly recurrence + run window (start/stop date) as
-- shows. Widen the trigger CHECK and add the window/time-of-day columns.
CREATE TABLE spot_rules_new (
    id            INTEGER PRIMARY KEY,
    folder_key    TEXT NOT NULL,
    label         TEXT,
    trigger       TEXT NOT NULL
        CHECK (trigger IN ('interval', 'clock', 'once', 'manual',
                           'daily', 'weekly')),
    interval_min  INTEGER,
    clock_minutes TEXT,
    start_at      TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_fired    REAL,
    created_at    REAL NOT NULL,
    file_path     TEXT,
    time_of_day   TEXT,
    days_mask     INTEGER,
    start_date    TEXT,
    end_date      TEXT
);
INSERT INTO spot_rules_new SELECT
    id, folder_key, label, trigger, interval_min, clock_minutes, start_at,
    enabled, last_fired, created_at, file_path, NULL, NULL, NULL, NULL
    FROM spot_rules;
DROP TABLE spot_rules;
ALTER TABLE spot_rules_new RENAME TO spot_rules;
"""),
    (7, """
-- Two-phase indexing: a fast path-only walk makes every file searchable in
-- minutes, then tags (artist/album/duration) are backfilled in the background.
-- tags_read = 0 means "path known, tags not read yet". Existing rows were fully
-- tag-read by the old single-pass indexer, so mark them done.
ALTER TABLE tracks ADD COLUMN tags_read INTEGER NOT NULL DEFAULT 0;
UPDATE tracks SET tags_read = 1;
CREATE INDEX idx_tracks_tags_read ON tracks(tags_read) WHERE tags_read = 0;
"""),
    (8, """
-- Station equipment to monitor. ICMP (ping) for now — TX, Barix, switches,
-- UniFi gear, etc. Status is computed live by a background pinger, not stored.
CREATE TABLE devices (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    host       TEXT NOT NULL,
    sort       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
"""),
    (9, """
-- Spots can target a browsed folder path (not just a preset station-folder
-- key), and choose how a file is picked from it: 'rotate' (round-robin) or
-- 'random'. folder_key stays for legacy rules.
ALTER TABLE spot_rules ADD COLUMN folder_path TEXT;
ALTER TABLE spot_rules ADD COLUMN pick_mode TEXT;   -- 'rotate' | 'random'
"""),
    (10, """
-- Playlists are ZaraRadio-style .lst files on disk: source_path is the exact
-- file a playlist was opened from (or first saved to) and every edit writes
-- straight back to it. The DB rows are the editor's working copy.
ALTER TABLE playlists ADD COLUMN source_path TEXT;
"""),
    (11, """
-- Each playlist item remembers its own length (seconds). Needed for the song
-- count / run time display and for time-remaining on air. The music index only
-- covers the NAS music root, so a .lst pointing elsewhere (e.g. a DJ's own
-- folders) would otherwise show no length at all — and saving the .lst would
-- blank out the durations Zara wrote into it. NULL = not known yet (the feeder
-- fills it in the first time the song is cached for air).
ALTER TABLE playlist_items ADD COLUMN duration_sec REAL;
"""),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def connect(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI runs sync endpoints in a threadpool and
    # may set up / tear down a request's connection on different threads. Each
    # request/job gets its OWN short-lived connection (never shared across
    # threads concurrently), so this is safe — and required to avoid spurious
    # "SQLite objects ... can only be used in that same thread" 500s under load.
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _integrity_ok(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return bool(row) and str(row[0]).strip().lower() == "ok"


def _prune_backups(bdir: str) -> None:
    """Keep only the last BACKUP_KEEP pre-migration snapshots (by mtime)."""
    try:
        files = [f for f in os.listdir(bdir)
                 if f.startswith("pre-migration-") and f.endswith(".db")]
    except OSError:
        return
    files.sort(key=lambda f: os.path.getmtime(os.path.join(bdir, f)))
    for f in files[:-BACKUP_KEEP]:
        try:
            os.remove(os.path.join(bdir, f))
        except OSError:
            pass


def _pre_migration_backup(db_path: str, old: int, new: int) -> str:
    """§6.4: before any pending migration runs, snapshot the DB into
    <dirname(db)>/backups/pre-migration-v<old>-to-v<new>-<stamp>.db with the
    SQLite backup API, then verify the COPY with PRAGMA integrity_check. If the
    copy or the check fails, raise (and remove the partial file) so the caller
    does NOT migrate — the live DB is left untouched. Prunes to BACKUP_KEEP."""
    bdir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups")
    os.makedirs(bdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(bdir, f"pre-migration-v{old}-to-v{new}-{stamp}")
    dst_path, n = base + ".db", 1
    while os.path.exists(dst_path):     # a retry within the same second
        n += 1
        dst_path = f"{base}-{n}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
        ok = _integrity_ok(dst)
    except sqlite3.Error as exc:
        dst.close()
        src.close()
        _rm(dst_path)
        raise RuntimeError(f"pre-migration backup failed ({exc})")
    dst.close()
    src.close()
    if not ok:
        _rm(dst_path)
        raise RuntimeError("pre-migration backup failed its integrity check")
    _prune_backups(bdir)
    log.info("pre-migration backup written: %s", dst_path)
    return dst_path


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def migrate(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_version
                        (version INTEGER NOT NULL)""")
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current = row["version"] if row else 0
        # §6.4: back up + verify the DB BEFORE touching the schema, but only
        # when there is actually a pending migration. A failed/unverifiable
        # backup aborts the whole migrate (raises) — never migrate blind.
        if MIGRATIONS and MIGRATIONS[-1][0] > current:
            _pre_migration_backup(db_path, current, MIGRATIONS[-1][0])
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            log.info("migrating schema %d -> %d", current, version)
            with conn:  # transaction per migration
                conn.executescript(script)
                conn.execute("DELETE FROM schema_version")
                conn.execute("INSERT INTO schema_version VALUES (?)",
                             (version,))
            current = version
    finally:
        conn.close()


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, value))


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?",
                       (key,)).fetchone()
    return row["value"] if row else default


def now() -> float:
    return time.time()
