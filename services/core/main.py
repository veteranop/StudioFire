"""StudioFire P2 — Core / Web Service entry point.

Web GUI + auth, playlist CRUD, scheduler/time events. Feeds P1 its queue over
localhost IPC and fills the local precache dir from the NAS (temp + verify +
atomic rename + manifest). Resolves dynamic playlist items (file /
folder-newest / folder-rotation) into concrete local files at precache time —
P1 only ever sees local paths. Ingests P1's play journal into SQLite.

May die at any time: audio is unaffected (see PLAN.md §5 crash matrix).

Run: python -m services.core.main [path/to/config.json]
Deployed as a Windows service via NSSM.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

import uvicorn

from .app import create_app
from .config import load_config

log = logging.getLogger("core")


def setup_logging(logs_dir: str) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(logs_dir, "core_error.log"),
        maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.WARNING)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


def main() -> int:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    setup_logging(cfg["logs_dir"])
    log.info("StudioFire core starting (pid %d) on %s:%d",
             os.getpid(), cfg["bind_host"], cfg["bind_port"])
    app = create_app(cfg)
    uvicorn.run(app, host=cfg["bind_host"], port=cfg["bind_port"],
                log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
