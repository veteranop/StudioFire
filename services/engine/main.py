"""StudioFire P1 — Audio Engine Service entry point.

THE ONLY PROCESS THAT MUST STAY ALIVE. Binding spec: PLAN.md §10.
STDLIB ONLY. The sole external dependency is mpv.exe (JSON IPC).

Run:  python -m services.engine.main [path/to/config.json]
Deployed as a Windows service via NSSM (auto-restart + heartbeat file).

Reads only the "engine"/"paths" sections of config.json; missing config
falls back to safe defaults rooted next to the repo so a botched config
can never prevent the engine from starting (it will sit in emergency
mode and scream in the logs instead).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import sys
import time

from .control import ControlServer
from .supervisor import EngineSupervisor

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
log = logging.getLogger("engine")


def load_config(path: str | None) -> dict:
    cfg = {}
    candidates = [path] if path else [os.path.join(ROOT, "config", "config.json")]
    for p in candidates:
        try:
            with open(p, "rb") as f:
                cfg = json.load(f)
            log.info("config loaded from %s", p)
            break
        except (OSError, ValueError) as exc:
            log.error("config %s unusable (%s) — using defaults", p, exc)
    paths = cfg.get("paths", {})
    engine = cfg.get("engine", {})
    data_dir = paths.get("data_dir", os.path.join(ROOT, "data"))
    logs_dir = paths.get("logs_dir", os.path.join(ROOT, "logs"))
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)
    return {
        "mpv_path": engine.get("mpv_path", os.path.join(ROOT, "bin", "mpv.exe")),
        "pipe_name": engine.get("pipe_name", "studiofire-engine"),
        "audio_device": engine.get("audio_device_guid") or None,
        "watchdog_interval": engine.get("watchdog_interval_sec", 1.0),
        "ipc_host": engine.get("ipc_host", "127.0.0.1"),
        "ipc_port": int(engine.get("ipc_port", 7701)),
        "emergency_dir": paths.get("emergency_dir",
                                   os.path.join(ROOT, "assets", "emergency")),
        "baked_in_asset": engine.get("baked_in_asset"),
        "state_path": os.path.join(data_dir, "queue_state.json"),
        "journal_path": os.path.join(logs_dir, "play_journal.jsonl"),
        "heartbeat_path": os.path.join(data_dir, "engine_heartbeat.txt"),
        "logs_dir": logs_dir,
        "extra_mpv_args": engine.get("extra_mpv_args", []),
    }


def setup_logging(logs_dir: str) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(logs_dir, "engine_error.log"),
        maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.WARNING)   # the error log holds warnings and up
    fh.setFormatter(fmt)
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)


def main() -> int:
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    setup_logging(cfg["logs_dir"])
    log.info("StudioFire engine starting (pid %d)", os.getpid())

    supervisor = EngineSupervisor(cfg)
    supervisor.start()
    control = ControlServer(supervisor, cfg["ipc_host"], cfg["ipc_port"])
    control.start()

    stop = []
    signal.signal(signal.SIGINT, lambda *a: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    try:
        while not stop:
            time.sleep(0.5)
    finally:
        log.info("engine shutting down")
        control.stop()
        supervisor.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
