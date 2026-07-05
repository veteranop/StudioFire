"""P2 config loader.

Reads the same config/config.json as the other services but only the
sections P2 cares about. Missing/broken config falls back to safe defaults
rooted next to the repo — P2 must always be able to start and show the GUI
(even if only to tell the operator the config is broken).
"""

from __future__ import annotations

import json
import logging
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
log = logging.getLogger("core.config")


def load_config(path: str | None = None) -> dict:
    cfg = {}
    p = path or os.path.join(ROOT, "config", "config.json")
    try:
        with open(p, "rb") as f:
            cfg = json.load(f)
        log.info("config loaded from %s", p)
    except (OSError, ValueError) as exc:
        log.error("config %s unusable (%s) — using defaults", p, exc)
    paths = cfg.get("paths", {})
    core = cfg.get("core", {})
    engine = cfg.get("engine", {})
    data_dir = paths.get("data_dir", os.path.join(ROOT, "data"))
    logs_dir = paths.get("logs_dir", os.path.join(ROOT, "logs"))
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)
    return {
        "station_name": cfg.get("station_name", "StudioFire"),
        "nas_music_root": paths.get("nas_music_root", ""),
        # {"\\\\SERVER\\share": "Z:"} — rewrites imported playlist paths so
        # a .lst written on another machine resolves locally
        "path_aliases": paths.get("path_aliases", {}),
        "precache_dir": paths.get("precache_dir",
                                  os.path.join(ROOT, "precache")),
        "emergency_dir": paths.get("emergency_dir",
                                   os.path.join(ROOT, "assets", "emergency")),
        "data_dir": data_dir,
        "logs_dir": logs_dir,
        "db_path": os.path.join(data_dir, "studiofire.db"),
        "secret_path": os.path.join(data_dir, "web_secret.key"),
        "bind_host": core.get("bind_host", "0.0.0.0"),
        "bind_port": int(core.get("bind_port", 8080)),
        # dev/dry-run convenience: a GUI "restart everything" button. Set this
        # to false on the ON-AIR PC so nobody can drop audio from the browser.
        "allow_gui_restart": bool(core.get("allow_gui_restart", True)),
        "precache_target_minutes": int(core.get("precache_target_minutes", 45)),
        "engine_url": "http://%s:%d" % (engine.get("ipc_host", "127.0.0.1"),
                                        int(engine.get("ipc_port", 7701))),
        "journal_path": os.path.join(logs_dir, "play_journal.jsonl"),
    }
