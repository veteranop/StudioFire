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

# Product telemetry: GA4 measurement ID baked into every install so feature
# usage across customer stations lands in one property (station_name rides
# along as a user property). A station can override with its own ID — or
# disable entirely with  "core": {"ga_measurement_id": ""}  — in config.json.
# The GUI works identically offline; the tag just never loads.
GA_MEASUREMENT_ID_DEFAULT = "G-3BK3GZDSJN"  # StudioFire GA4 property (VeteranOp)


# Now-playing push (#747): off-box, feature-flagged, fire-and-forget pusher
# that mirrors StudioFire's current title to the stream CDN's metadata ingest
# (e.g. Icecast `updinfo`) so the public player shows song info. OFF by
# default; the real transport is gated on StreamGuys' written answer — until
# then the only transport is "dryrun" (logs, no I/O). See
# docs/NOWPLAYING-CONSENSUS.md. HARD RULE: the auth secret is NEVER read from
# config.json — only from the env var named by `auth_env`. Keep
# config.example.json free of any real credential.
NOWPLAYING_AUTH_ENV_DEFAULT = "STUDIOFIRE_NOWPLAYING_AUTH"


def _load_nowplaying_push(cfg: dict) -> dict:
    np = cfg.get("nowplaying_push", {}) or {}
    auth_env = str(np.get("auth_env", NOWPLAYING_AUTH_ENV_DEFAULT))
    # Secret is env-sourced only. Even if someone drops an "auth" key into
    # config.json it is deliberately ignored — secrets never live in git.
    auth = os.environ.get(auth_env) or None
    return {
        "enabled": bool(np.get("enabled", False)),
        "transport": str(np.get("transport", "dryrun")),
        "endpoint": str(np.get("endpoint", "")),
        "mount": str(np.get("mount", "")),
        "auth_env": auth_env,
        "auth": auth,
        "timeout_sec": float(np.get("timeout_sec", 4.0)),
        "min_interval_sec": float(np.get("min_interval_sec", 5.0)),
        "include_spots": bool(np.get("include_spots", False)),
    }


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
        "ga_measurement_id": core.get("ga_measurement_id",
                                      GA_MEASUREMENT_ID_DEFAULT),
        "precache_target_minutes": int(core.get("precache_target_minutes", 45)),
        # how many tracks P1's queue itself is fed ahead of the play-head —
        # deliberately shallow; precache_target_minutes (disk cache) is what
        # actually protects against a NAS outage (feeder hardening)
        "feed_ahead_tracks": int(core.get("feed_ahead_tracks", 3)),
        # look for a newer StudioFire release on GitHub every few hours and
        # offer it in Settings (never installs on its own)
        "update_check": bool(core.get("update_check", True)),
        "engine_url": "http://%s:%d" % (engine.get("ipc_host", "127.0.0.1"),
                                        int(engine.get("ipc_port", 7701))),
        "journal_path": os.path.join(logs_dir, "play_journal.jsonl"),
        "nowplaying_push": _load_nowplaying_push(cfg),
    }
