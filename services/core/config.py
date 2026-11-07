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
# default; the real transport is gated on the stream host's written answer —
# until then the only transport is "dryrun" (logs, no I/O). See
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


ROYALTY_DEFAULTS = {
    # No default for service_name: a Report of Use filed under the wrong
    # station's name is worse than no report. The generator refuses to run
    # until the station sets it.
    "service_name": "",
    "filer_name": "",
    "contact": {"name": "", "street": "", "city_state_zip": "",
                "phone": "", "email": ""},
    "category": "B",              # A webcast | B broadcast simulcast | C talk
    "mode": "census_atp",         # census_atp | sample_atp | sample_ath
    "channel_name": "",           # falls back to service_name
    "output_format": "headerless",  # headerless | headered
    "share_aliases": [],          # equivalent share roots, e.g. ["\\NAS\music"]
    "music_drive_letter": "",     # e.g. "Z:" when the share is mapped
}


def _load_royalty_reporting(cfg: dict) -> dict:
    """The royalty-reporting block, with safe defaults.

    Everything station-specific (name, contact, shares, drive letter) comes
    from here — nothing about one station's network is baked into the code.
    """
    raw = cfg.get("royalty_reporting") or {}
    out = {k: raw.get(k, v) for k, v in ROYALTY_DEFAULTS.items()
           if k != "contact"}
    contact = dict(ROYALTY_DEFAULTS["contact"])
    contact.update({str(k): str(v or "") for k, v in
                    (raw.get("contact") or {}).items()})
    out["contact"] = contact
    out["service_name"] = str(out.get("service_name") or "").strip()
    out["filer_name"] = str(out.get("filer_name") or "").strip()
    out["category"] = str(out.get("category") or "B").strip().upper()
    out["mode"] = str(out.get("mode") or "census_atp").strip()
    out["channel_name"] = str(out.get("channel_name") or "").strip()
    out["output_format"] = str(out.get("output_format")
                               or "headerless").strip()
    out["share_aliases"] = [str(x) for x in (out.get("share_aliases") or [])
                            if str(x).strip()]
    out["music_drive_letter"] = str(out.get("music_drive_letter") or "").strip()
    return out


def save_royalty_reporting(block: dict, path: str | None = None) -> dict:
    """Merge royalty-reporting values into config.json and return the result.

    Only the keys given are touched, so saving the Settings card never wipes a
    hand-edited share alias. Written atomically (tmp + replace) because the
    file is read by the on-air services.
    """
    import json as _json
    p = path or os.path.join(ROOT, "config", "config.json")
    try:
        with open(p, encoding="utf-8") as fh:
            cfg = _json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    rr = cfg.get("royalty_reporting") or {}
    for k, v in (block or {}).items():
        if k == "contact":
            contact = rr.get("contact") or {}
            contact.update({str(a): str(b or "") for a, b in (v or {}).items()})
            rr["contact"] = contact
        elif k in ROYALTY_DEFAULTS:
            rr[k] = v
    cfg["royalty_reporting"] = rr
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        _json.dump(cfg, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    return _load_royalty_reporting(cfg)


def _default_data_root() -> str:
    r"""Machine-wide data folder on Windows, beside the app everywhere else.

    %ProgramData%\StudioFire keeps the DB, logs, precache and backups outside
    the install directory, so an upgrade or uninstall-with-keep-data cannot lose
    them. A developer checkout without ProgramData just uses its own folder.
    """
    pd = os.environ.get("ProgramData")
    if os.name == "nt" and pd:
        try:
            root = os.path.join(pd, "StudioFire")
            os.makedirs(root, exist_ok=True)
            return root
        except OSError:
            pass
    return ROOT


def update_config(block: dict, path: str | None = None) -> dict:
    """Merge nested sections into config.json atomically.

    The setup wizard writes here (station name, music root, audio device); the
    file is also read by the on-air services, so a half-written config is not an
    option. Unknown sections are preserved untouched.
    """
    import json as _json
    p = path or os.path.join(ROOT, "config", "config.json")
    try:
        with open(p, encoding="utf-8") as fh:
            cfg = _json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    for section, values in (block or {}).items():
        if isinstance(values, dict):
            cur = cfg.get(section)
            if not isinstance(cur, dict):
                cur = {}
            cur.update(values)
            cfg[section] = cur
        else:
            cfg[section] = values
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        _json.dump(cfg, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    return cfg


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
    # Where a station's own data lives. On Windows that is
    # %ProgramData%\StudioFire (machine-wide, survives upgrades, outside the
    # install folder); elsewhere it stays beside the app. Both are overridable
    # per-install via paths.data_root / data_dir / logs_dir, which is what the
    # reference station does so its existing data is not moved.
    base = paths.get("data_root") or _default_data_root()
    data_dir = paths.get("data_dir", os.path.join(base, "data"))
    logs_dir = paths.get("logs_dir", os.path.join(base, "logs"))
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)
    return {
        "station_name": cfg.get("station_name", "StudioFire"),
        "nas_music_root": paths.get("nas_music_root", ""),
        # {"\\\\SERVER\\share": "Z:"} — rewrites imported playlist paths so
        # a .lst written on another machine resolves locally
        "path_aliases": paths.get("path_aliases", {}),
        # [{"share": "\\\\NAS\\Music", "drive": "Z:"}] — what the setup wizard
        # fills in when a folder is picked through a mapped drive. Distinct from
        # path_aliases (which rewrites imported .lst paths).
        "share_aliases": paths.get("share_aliases", []),
        "precache_dir": paths.get("precache_dir",
                                  os.path.join(base, "precache")),
        "emergency_dir": paths.get("emergency_dir",
                                   os.path.join(base, "emergency")),
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
        "royalty_reporting": _load_royalty_reporting(cfg),
    }
