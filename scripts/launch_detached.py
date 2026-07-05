"""Launch P1/P2/P3 fully detached (survive the parent shell), logging each to
logs/<svc>_console.log. Used to keep the dev stack alive across sessions.
Run: python scripts/launch_detached.py
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

for mod, delay in [("services.engine.main", 2.0),
                   ("services.core.main", 1.0),
                   ("services.worker.main", 0.0)]:
    name = mod.split(".")[1]
    logf = open(os.path.join("logs", name + "_console.log"), "ab")
    p = subprocess.Popen([PY, "-m", mod, CFG], stdout=logf, stderr=logf,
                         stdin=subprocess.DEVNULL, creationflags=FLAGS,
                         close_fds=True)
    print(f"launched {mod} (pid {p.pid})")
    time.sleep(delay)
