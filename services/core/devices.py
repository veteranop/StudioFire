"""Station equipment monitor — ICMP reachability (ping) for now.

Devices (TX, Barix, switches, UniFi gear, …) are stored in the `devices` table;
their up/down status is computed live by a background pinger and held in memory
(never persisted — it's ephemeral health, like the health tiles).

Ping is done by shelling out to the OS `ping` (one echo, short timeout): no raw
sockets / admin rights needed, and it's gentle. The pinger runs on a low
cadence so it never competes with playback.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import threading
import time

PING_INTERVAL = 20.0     # seconds between sweeps of all devices
PING_TIMEOUT_MS = 1500   # per-device wait
_WIN = sys.platform.startswith("win")
# CREATE_NO_WINDOW: keep the ping subprocess from flashing a console window on
# Windows (P2 runs with no console, so each ping would otherwise pop one up).
_NO_WINDOW = 0x08000000 if _WIN else 0
# grab the reported round-trip time from ping output (Windows 'time=12ms' /
# 'time<1ms'; unix 'time=12.3 ms')
_RTT = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)


# ---------------------------------------------------------------------- CRUD

def add(conn: sqlite3.Connection, name: str, host: str) -> int:
    with conn:
        sort = conn.execute(
            "SELECT COALESCE(MAX(sort) + 1, 0) FROM devices").fetchone()[0]
        cur = conn.execute(
            "INSERT INTO devices (name, host, sort, created_at) "
            "VALUES (?, ?, ?, ?)", (name, host, sort, time.time()))
    return cur.lastrowid


def remove(conn: sqlite3.Connection, did: int) -> None:
    with conn:
        conn.execute("DELETE FROM devices WHERE id = ?", (did,))


def list_devices(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, name, host FROM devices ORDER BY sort, id").fetchall()]


# ---------------------------------------------------------------------- ping

def ping(host: str, timeout_ms: int = PING_TIMEOUT_MS) -> tuple[bool, float | None]:
    """One ICMP echo. Returns (reachable, round-trip ms or None)."""
    if _WIN:
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), host]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout_ms / 1000 + 2,
                             creationflags=_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return False, None
    if out.returncode != 0:
        return False, None
    # Windows prints "Destination host unreachable" with returncode 0 sometimes
    if _WIN and "unreachable" in out.stdout.lower():
        return False, None
    m = _RTT.search(out.stdout)
    return True, (float(m.group(1)) if m else None)


class Pinger:
    """Background thread that sweeps all devices every PING_INTERVAL and keeps
    the latest status in memory. Read via status()."""

    def __init__(self, db_connect, db_path: str):
        self._connect = db_connect
        self._db_path = db_path
        self._status: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="pinger",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(0.1):
            try:
                conn = self._connect(self._db_path)
                try:
                    devices = list_devices(conn)
                finally:
                    conn.close()
                seen = set()
                for d in devices:
                    if self._stop.is_set():
                        break
                    up, rtt = ping(d["host"])
                    seen.add(d["id"])
                    with self._lock:
                        self._status[d["id"]] = {
                            "up": up, "latency_ms": rtt,
                            "checked_at": time.time()}
                with self._lock:  # forget removed devices
                    for did in list(self._status):
                        if did not in seen:
                            self._status.pop(did, None)
            except Exception:  # never let the monitor thread die
                pass
            self._stop.wait(PING_INTERVAL)

    def status(self, did: int) -> dict | None:
        with self._lock:
            return self._status.get(did)
