"""StudioFire P3 — Worker Service entry point.

Throttled, low-OS-priority background jobs, isolated so they can never touch
playback or the GUI:
- Library indexer: incremental NAS scan (mutagen tags -> SQLite).
- Syndication fetcher (Phase 3): SFTP/FTP show downloads, temp -> verify ->
  atomic rename, retention pruning.

Run: python -m services.worker.main [path/to/config.json]
Deployed as a Windows service via NSSM.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import sys
import time

from services.core import db as coredb
from services.core.config import load_config

from .indexer import scan

log = logging.getLogger("worker")

SCAN_INTERVAL = 15 * 60  # incremental rescan cadence (seconds)


def setup_logging(logs_dir: str) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(logs_dir, "worker_error.log"),
        maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.WARNING)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


def lower_priority() -> None:
    """Below-normal OS priority: the on-air PC's CPU belongs to playback."""
    try:
        import ctypes
        BELOW_NORMAL = 0x4000
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL)
    except Exception:  # non-Windows dev box — fine
        pass


def main() -> int:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    setup_logging(cfg["logs_dir"])
    lower_priority()
    log.info("StudioFire worker starting (pid %d)", os.getpid())
    coredb.migrate(cfg["db_path"])

    stop = []
    signal.signal(signal.SIGINT, lambda *a: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))

    root = cfg["nas_music_root"]
    while not stop:
        if root and os.path.isdir(root):
            conn = coredb.connect(cfg["db_path"])
            try:
                scan(conn, root, stop_check=lambda: bool(stop))
            except Exception:
                log.exception("scan pass failed — retrying next interval")
            finally:
                conn.close()
        else:
            log.warning("nas_music_root not set/reachable (%r) — idle", root)
        deadline = time.monotonic() + SCAN_INTERVAL
        while not stop and time.monotonic() < deadline:
            time.sleep(1)
    log.info("worker shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
