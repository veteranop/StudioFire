"""Restart all StudioFire services — console-independent.

The GUI restart button runs inside P2, which is launched detached (no console).
start-all.bat's `start "title" cmd /k ...` needs a console/desktop to spawn its
windows, so from that context the stop worked but the relaunch silently failed —
leaving the box on emergency filler with the web UI down. This helper avoids
consoles entirely: it kills the services and relaunches them with
DETACHED_PROCESS (the same way scripts/launch_detached.py does).

It is spawned detached by P2 and MUST survive P2 being killed, so it kills P2
(and P3) WITHOUT /T (a tree-kill of P2 would take this helper down with it — it
is P2's child). Only the engine is tree-killed, to also stop its mpv child.

Everything is logged to logs/restart.log so a failed restart is diagnosable.
Run manually too:  python scripts/restart_all.py
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
_logf = open(os.path.join("logs", "restart.log"), "a", encoding="utf-8")


def log(msg: str) -> None:
    _logf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    _logf.flush()


def service_pids() -> list[tuple[str, str]]:
    """[(pid, 'engine'|'core'|'worker'), ...] for the running services."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "ForEach-Object { if ($_.CommandLine -match "
          "'services\\.(engine|core|worker)\\.main') { "
          "\"$($_.ProcessId) $($Matches[1])\" } }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True)
    pairs = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            pairs.append((parts[0], parts[1]))
    return pairs


def console_host_pids() -> list[str]:
    """The `cmd /k` windows start-all.bat leaves hosting each service. They
    keep the *_console.log files open via their >> redirection, so they must
    die too or the relaunch can hit 'Permission denied' opening those logs
    (exactly what stranded the stack on 2026-07-08). Killing them WITHOUT /T
    is safe: this helper is a grandchild of core's cmd, and a plain kill
    takes only cmd itself."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='cmd.exe'\" | "
          "ForEach-Object { if ($_.CommandLine -match "
          "'services\\.(engine|core|worker)\\.main') { $_.ProcessId } }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True)
    return [ln.strip() for ln in out.stdout.splitlines()
            if ln.strip().isdigit()]


def open_log(name: str):
    """Console log for a relaunched service — but NEVER let a locked/broken
    log file stop the relaunch itself (dead air beats lost stdout)."""
    try:
        return open(os.path.join("logs", name + "_console.log"), "ab")
    except OSError as exc:
        log(f"could not open {name}_console.log ({exc}) — output discarded")
        return subprocess.DEVNULL


def nssm_managed() -> bool:
    """True when the stack runs as NSSM Windows services. Then THIS helper
    must not kill/relaunch anything: NSSM would revive what we kill (duplicate
    engines = double audio) and its stop logic tree-kills this helper mid-run.
    Service mode restarts go through services.msc or
    scripts\\restart-services.bat (admin)."""
    rc = subprocess.run(["sc", "query", "StudioFireEngine"],
                        capture_output=True)
    return rc.returncode == 0


def main() -> int:
    log(f"restart requested (python={PY})")
    if nssm_managed():
        log("stack is NSSM-managed — refusing; use services.msc or "
            "scripts\\restart-services.bat (run as Administrator)")
        return 1
    for pid, name in service_pids():
        # engine gets /T so its mpv child dies too; core/worker do NOT (a tree
        # kill of core would kill this helper, which is core's child).
        args = ["taskkill", "/F", "/PID", pid]
        if name == "engine":
            args.insert(2, "/T")
        subprocess.run(args, capture_output=True)
        log(f"stopped {name} (pid {pid})")
    for pid in console_host_pids():
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        log(f"closed leftover service console window (pid {pid})")

    time.sleep(2)

    failed = 0
    for mod, delay in [("services.engine.main", 2.0),
                       ("services.core.main", 1.0),
                       ("services.worker.main", 0.0)]:
        name = mod.split(".")[1]
        try:
            logf = open_log(name)
            p = subprocess.Popen([PY, "-m", mod, CFG], stdout=logf,
                                 stderr=logf, stdin=subprocess.DEVNULL,
                                 creationflags=FLAGS, close_fds=True)
            log(f"launched {mod} (pid {p.pid})")
        except Exception as exc:  # noqa: BLE001 — keep launching the rest
            failed += 1
            log(f"FAILED to launch {mod}: {exc}")
        time.sleep(delay)
    log("restart complete" + (f" ({failed} launch failure(s))" if failed
                              else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
