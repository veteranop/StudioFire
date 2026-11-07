"""Where StudioFire keeps its files — one answer for both services.

The engine (P1) and the core/web service (P2) must agree, or the station quietly
breaks in ways that look like something else: the engine writes the play journal
and its heartbeat where the core does not look (so reports go empty and a hung
engine is never faulted), and the engine's emergency tier reads a folder the
setup wizard never writes.

This module is deliberately **stdlib-only**: P1 must stay dependency-free. Both
services call `resolve()` with their own repo root and the `paths` block from
config.json.

Precedence, per key: `paths.<key>` → `paths.data_root` → `%ProgramData%\\StudioFire`
on Windows (machine-wide, survives upgrades, outside the install folder) → the
install folder itself (a dev checkout with no ProgramData).
"""

from __future__ import annotations

import os
import shutil

__all__ = ["default_data_root", "resolve", "seed_emergency", "ensure_dirs"]

DATA_FILE_NAMES = ("queue_state.json", "engine_heartbeat.txt",
                   "studiofire.db", "web_secret.key")


def default_data_root(app_root: str) -> str:
    """Machine-wide data folder on Windows, beside the app everywhere else."""
    programdata = os.environ.get("ProgramData")
    if os.name == "nt" and programdata:
        return os.path.join(programdata, "StudioFire")
    return app_root


def resolve(paths: dict | None, app_root: str) -> dict:
    """Every path both services need, resolved identically.

    `paths` is config.json's `paths` block (may be empty or None).
    """
    paths = paths or {}
    base = (paths.get("data_root") or "").strip() or default_data_root(app_root)
    # Legacy layout insurance: an install that already keeps its database in
    # <app>/data and whose config pins nothing (a reset or hand-edited config)
    # must NOT silently start using an empty database in the machine-wide
    # folder — that looks exactly like total data loss. Detected, not written:
    # resolve() never mutates a station's config.
    if not (paths.get("data_root") or "").strip() \
            and not (paths.get("data_dir") or "").strip():
        try:
            if os.path.isfile(os.path.join(app_root, "data", "studiofire.db")):
                base = app_root
        except OSError:
            pass
    data_dir = (paths.get("data_dir") or "").strip() or os.path.join(base, "data")
    logs_dir = (paths.get("logs_dir") or "").strip() or os.path.join(base, "logs")
    precache_dir = ((paths.get("precache_dir") or "").strip()
                    or os.path.join(base, "precache"))
    emergency_dir = ((paths.get("emergency_dir") or "").strip()
                     or os.path.join(base, "emergency"))
    return {
        "data_root": base,
        "data_dir": data_dir,
        "logs_dir": logs_dir,
        "precache_dir": precache_dir,
        "emergency_dir": emergency_dir,
        "state_path": os.path.join(data_dir, "queue_state.json"),
        "journal_path": os.path.join(logs_dir, "play_journal.jsonl"),
        "heartbeat_path": os.path.join(data_dir, "engine_heartbeat.txt"),
    }


def ensure_dirs(resolved: dict) -> None:
    """Create the folders a service writes to. Never raises for a read-only box."""
    for key in ("data_dir", "logs_dir", "precache_dir", "emergency_dir"):
        path = resolved.get(key)
        if not path:
            continue
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass


def seed_emergency(resolved: dict, app_root: str) -> int:
    """Copy the shipped emergency clips into the emergency folder once.

    Returns how many files were copied (0 when the folder already has audio or
    the install ships none). The engine's last-resort tier reads this folder, so
    a fresh install must have something in it that both services agree on.
    """
    dest = resolved.get("emergency_dir")
    if not dest:
        return 0
    try:
        if os.path.isdir(dest) and any(
                f.lower().endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg"))
                for f in os.listdir(dest)):
            return 0
    except OSError:
        return 0
    src = os.path.join(app_root, "assets", "emergency")
    if not os.path.isdir(src):
        return 0
    copied = 0
    try:
        os.makedirs(dest, exist_ok=True)
        for name in os.listdir(src):
            source = os.path.join(src, name)
            if not os.path.isfile(source):
                continue
            target = os.path.join(dest, name)
            if not os.path.exists(target):
                shutil.copy2(source, target)
                copied += 1
    except OSError:
        return copied
    return copied
