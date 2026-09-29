"""Launch P1/P2/P3 fully detached (survive the parent shell), logging each to
logs/<svc>_console.log. Used to keep the dev stack alive across sessions.
Run: python scripts/launch_detached.py            # all three
     python scripts/launch_detached.py core        # just P2 (engine/worker names too)
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
os.makedirs("logs", exist_ok=True)
PY = sys.executable
CFG = os.path.join("config", "config.json")
FLAGS = (getattr(subprocess, "DETACHED_PROCESS", 0)
         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def map_drives() -> None:
    """Legacy Z:\\ data still works literally: a detached process does NOT
    inherit the logged-in session's mapped drives, so run config\\drive-map.bat
    (if the station has one) before starting the services. Never fail the launch
    on it — dead air beats a mapping error."""
    bat = os.path.join("config", "drive-map.bat")
    if os.path.exists(bat):
        try:
            subprocess.run(["cmd", "/c", bat], capture_output=True, timeout=30)
            print("ran config\\drive-map.bat")
        except Exception as exc:  # noqa: BLE001 — mapping is best-effort
            print(f"[!] config\\drive-map.bat failed ({exc}); continuing")


map_drives()
want = set(a.lower() for a in sys.argv[1:])  # e.g. {"core"}; empty = launch all
for mod, delay in [("services.engine.main", 2.0),
                   ("services.core.main", 1.0),
                   ("services.worker.main", 0.0)]:
    name = mod.split(".")[1]
    if want and name not in want:
        continue
    try:
        logf = open(os.path.join("logs", name + "_console.log"), "ab")
    except OSError as exc:  # a stale console window may hold the log open —
        print(f"[!] {name}_console.log locked ({exc}); output discarded")
        logf = subprocess.DEVNULL  # launching beats logging

    p = subprocess.Popen([PY, "-m", mod, CFG], stdout=logf, stderr=logf,
                         stdin=subprocess.DEVNULL, creationflags=FLAGS,
                         close_fds=True)
    print(f"launched {mod} (pid {p.pid})")
    time.sleep(delay)
