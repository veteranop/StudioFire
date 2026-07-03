"""P2 database layer — SQLite in WAL mode, versioned schema (PLAN.md §5).

P1 NEVER touches this database (design law). Migrations run at startup;
schema_version is tracked in-DB so updates can migrate old deployments
automatically (PLAN.md Phase 4 depends on this existing from day one).

Connections are cheap and short-lived: one per request/job via connect().
WAL mode makes concurrent readers + one writer safe across P2/P3/P4.
"""

from __future__ import annotations

import logging
import sqlite3
import time

log = logging.getLogger("core.db")

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
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def migrate(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_version
                        (version INTEGER NOT NULL)""")
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current = row["version"] if row else 0
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
