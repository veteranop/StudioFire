"""Append-only JSONL play journal for P1. PLAN.md §10.4.

P1 design laws apply: STDLIB ONLY.

P1 writes every play event here the INSTANT it happens — this file, not
P2's database, is the source of truth for what actually aired. P2 ingests
on (re)connect (merge by time, dedupe by event id); P2 downtime can never
lose as-aired/affidavit data. Lines are human-readable JSON for emergency
manual review.

Event shape (one JSON object per line):
    {"id": "1751500000123-4", "ts": "2026-07-03T14:30:00.123-05:00",
     "event": "track_start" | "track_end" | "emergency_enter" |
              "emergency_exit" | "engine_start" | "engine_stop" |
              "mpv_restart" | "device_error",
     ...event-specific fields (path, title, source, reason, duration)}

Durability: flush + fsync per event. Events happen at track boundaries
(every few minutes) — fsync cost is irrelevant, lost airplay proof is not.

Rotation: when the active file exceeds max_bytes it is renamed to
play_journal.<utc-stamp>.jsonl in the same directory. Rotated files are
never deleted by P1 — cleanup happens in P2 only AFTER successful ingest.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
import time

log = logging.getLogger("engine.journal")


class Journal:
    """Thread-safe append-only JSONL journal with size-based rotation."""

    def __init__(self, path: str, max_bytes: int = 10 * 1024 * 1024):
        self._path = path
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._seq = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._file = open(self._path, "ab")
        # Repair a torn tail (power loss mid-write): if the last byte isn't a
        # newline, terminate it so the next append can't merge into garbage.
        if self._file.tell() > 0:
            with open(self._path, "rb") as rf:
                rf.seek(-1, os.SEEK_END)
                if rf.read(1) != b"\n":
                    self._file.write(b"\n")
                    self._file.flush()

    def append(self, event: str, **fields) -> dict:
        """Write one event. Never raises into playback code paths."""
        now = time.time()
        with self._lock:
            self._seq += 1
            record = {
                "id": f"{int(now * 1000)}-{self._seq}",
                "ts": datetime.datetime.now().astimezone().isoformat(
                    timespec="milliseconds"),
                "event": event,
                **fields,
            }
            try:
                line = json.dumps(record, ensure_ascii=False) + "\n"
                self._file.write(line.encode("utf-8"))
                self._file.flush()
                os.fsync(self._file.fileno())
                if self._file.tell() >= self._max_bytes:
                    self._rotate()
            except OSError:
                # Journal failure must NEVER take down playback. Log and go on;
                # the supervisor surfaces disk problems via its own health checks.
                log.exception("journal write failed (event=%s)", event)
            return record

    def close(self) -> None:
        with self._lock:
            try:
                self._file.close()
            except OSError:
                pass

    # ---------------------------------------------------------------- internal

    def _rotate(self) -> None:
        self._file.close()
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
        base, ext = os.path.splitext(self._path)
        target = f"{base}.{stamp}{ext}"
        n = 1
        while os.path.exists(target):  # same-second rotation collision
            target = f"{base}.{stamp}-{n}{ext}"
            n += 1
        os.replace(self._path, target)
        self._file = open(self._path, "ab")
        log.info("journal rotated to %s", os.path.basename(target))


def read_events(path: str):
    """Yield parsed events from a journal file, skipping any torn last line.

    (A torn final line is possible only if power died mid-write; fsync per
    event means at most one line can ever be incomplete.)
    """
    with open(path, "rb") as f:
        for raw in f:
            try:
                yield json.loads(raw)
            except ValueError:
                log.warning("skipping torn/corrupt journal line in %s", path)
