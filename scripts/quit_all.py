"""Quit all StudioFire services (kill without restart).

This helper stops all running services cleanly and exits. It is spawned detached
by P2 and must survive P2 being killed.

Engine is tree-killed to also stop its mpv child. Core/Worker are killed without
tree-kill (to avoid killing this helper, which is their child).

Everything is logged to logs/quit.log.
Run manually too:  python scripts/quit_all.py
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
os.makedirs("logs", exist_ok=True)
_logf = open(os.path.join("logs", "quit.log"), "a", encoding="utf-8")


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


def main() -> int:
    log("quit requested — stopping all services")
    for pid, name in service_pids():
        # engine gets /T so its mpv child dies too; core/worker do NOT (a tree
        # kill of core would kill this helper, which is core's child).
        args = ["taskkill", "/F", "/PID", pid]
        if name == "engine":
            args.insert(2, "/T")
        subprocess.run(args, capture_output=True)
        log(f"stopped {name} (pid {pid})")

    log("all services stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
