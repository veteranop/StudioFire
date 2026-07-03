"""mpv process manager + JSON IPC client over a Windows named pipe.

P1 design laws apply: STDLIB ONLY (ctypes counts). See PLAN.md §10.

WINDOWS PIPE GOTCHA (learned the hard way, do not regress):
A synchronous named-pipe handle serializes I/O — while one thread sits in a
blocking ReadFile (readline), another thread's WriteFile blocks until that
read completes. Reader-thread + writer-thread on one handle = deadlock.
Therefore this client NEVER issues a blocking read: a single I/O thread owns
the pipe, polls PeekNamedPipe for available bytes, reads only what exists,
and drains an outgoing write queue between polls. All I/O on one thread.

Responsibilities:
- Launch/terminate bin/mpv.exe with radio-appropriate flags (audio only,
  gapless, prefetch so the next track is primed before the current ends).
- JSON IPC: request/response correlation via request_id, with timeouts.
- Async events delivered to a caller-supplied callback on the I/O thread
  (callbacks must be quick and never block).
- Liveness primitives for the supervisor watchdog: is_running(), ping().

This module contains NO failover or queue logic. It is a dumb, reliable
pipe to mpv. Policy lives in supervisor.py.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import msvcrt
import queue
import subprocess
import threading
import time

log = logging.getLogger("engine.mpv")

PIPE_PREFIX = "\\\\.\\pipe\\"
POLL_INTERVAL = 0.005  # 5ms I/O loop; fine for control-plane latency

_kernel32 = ctypes.windll.kernel32


def _bytes_available(pipe_file) -> int:
    """PeekNamedPipe: how many bytes can be read without blocking."""
    handle = msvcrt.get_osfhandle(pipe_file.fileno())
    avail = ctypes.wintypes.DWORD(0)
    ok = _kernel32.PeekNamedPipe(
        ctypes.wintypes.HANDLE(handle), None, 0, None,
        ctypes.byref(avail), None,
    )
    if not ok:
        raise OSError("PeekNamedPipe failed (pipe broken), winerr=%d"
                      % ctypes.GetLastError())
    return avail.value


class MpvError(Exception):
    """mpv rejected a command (response error != 'success')."""


class MpvDead(Exception):
    """mpv process or IPC pipe is gone; caller should restart via supervisor."""


class MpvTimeout(MpvDead):
    """No reply within timeout — treat as dead/hung (silent-hang symptom)."""


class MpvClient:
    """One mpv process + its IPC connection. Not reusable after stop()."""

    def __init__(
        self,
        mpv_path: str,
        pipe_name: str,
        audio_device: str | None = None,
        volume: int = 100,
        extra_args: list[str] | None = None,
        event_callback=None,
    ):
        self._mpv_path = mpv_path
        self._pipe_path = PIPE_PREFIX + pipe_name
        self._audio_device = audio_device
        self._volume = volume
        self._extra_args = list(extra_args or [])
        self._event_callback = event_callback

        self._proc: subprocess.Popen | None = None
        self._pipe = None
        self._io_thread: threading.Thread | None = None
        self._outbox: queue.Queue[bytes] = queue.Queue()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, dict] = {}  # request_id -> {"event": Event, "resp": ...}
        self._next_id_lock = threading.Lock()
        self._next_request_id = 1
        self._closed = threading.Event()

    # ------------------------------------------------------------------ setup

    def start(self, connect_timeout: float = 5.0) -> None:
        args = [
            self._mpv_path,
            "--no-config",
            "--no-video",
            "--no-terminal",
            "--idle=yes",
            "--keep-open=no",
            "--gapless-audio=yes",
            "--prefetch-playlist=yes",   # prime next track before current ends (§10.1)
            "--volume=" + str(self._volume),
            "--input-ipc-server=" + self._pipe_path,
        ]
        if self._audio_device:
            args.append("--audio-device=" + self._audio_device)
        args += self._extra_args

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(args, creationflags=creationflags)

        deadline = time.monotonic() + connect_timeout
        while True:
            if self._proc.poll() is not None:
                raise MpvDead(f"mpv exited on startup (code {self._proc.returncode})")
            try:
                self._pipe = open(self._pipe_path, "r+b", buffering=0)
                break
            except OSError:
                if time.monotonic() > deadline:
                    self._kill_process()
                    raise MpvTimeout("IPC pipe never appeared: " + self._pipe_path)
                time.sleep(0.05)

        self._io_thread = threading.Thread(
            target=self._io_loop, name="mpv-ipc-io", daemon=True
        )
        self._io_thread.start()
        log.info("mpv started pid=%s pipe=%s", self._proc.pid, self._pipe_path)

    # ------------------------------------------------------------- public API

    def command(self, *args, timeout: float = 2.0):
        """Send a command, wait for its reply. Returns response 'data'.

        Raises MpvError on mpv-rejected commands, MpvTimeout on no reply,
        MpvDead if the process/pipe is gone.
        """
        if self._closed.is_set():
            raise MpvDead("client is closed")
        with self._next_id_lock:
            request_id = self._next_request_id
            self._next_request_id += 1
        waiter = {"event": threading.Event(), "resp": None}
        with self._pending_lock:
            self._pending[request_id] = waiter

        payload = json.dumps({"command": list(args), "request_id": request_id})
        self._outbox.put(payload.encode("utf-8") + b"\n")

        if not waiter["event"].wait(timeout):
            self._forget(request_id)
            raise MpvTimeout(f"no reply in {timeout}s: {args[0]}")

        resp = waiter["resp"]
        if resp is None:  # I/O thread shut down while we waited
            raise MpvDead("IPC connection closed while awaiting reply")
        if resp.get("error") != "success":
            raise MpvError(f"{args}: {resp.get('error')}")
        return resp.get("data")

    def get_property(self, name: str, timeout: float = 2.0):
        return self.command("get_property", name, timeout=timeout)

    def set_property(self, name: str, value, timeout: float = 2.0):
        return self.command("set_property", name, value, timeout=timeout)

    def observe_property(self, name: str, timeout: float = 2.0) -> int:
        """Subscribe to property-change events (delivered via event_callback)."""
        with self._next_id_lock:
            observe_id = self._next_request_id
            self._next_request_id += 1
        self.command("observe_property", observe_id, name, timeout=timeout)
        return observe_id

    def ping(self, timeout: float = 1.0) -> bool:
        """Cheap IPC liveness check for the 1s watchdog."""
        try:
            self.command("get_property", "pid", timeout=timeout)
            return True
        except MpvDead:
            return False
        except MpvError:
            return True  # it answered — alive, even if it complained

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self, timeout: float = 3.0) -> None:
        """Graceful quit, then hard kill. Safe to call repeatedly."""
        if self.is_running():
            self._outbox.put(b'{"command":["quit"]}\n')
            deadline = time.monotonic() + timeout
            while self.is_running() and time.monotonic() < deadline:
                time.sleep(0.05)
        self._closed.set()  # I/O thread exits its loop
        self._kill_process()
        if self._io_thread is not None:
            self._io_thread.join(2)
        self._close_pipe()
        self._fail_all_pending()

    # ---------------------------------------------------------------- internals

    def _io_loop(self) -> None:
        """Single owner of all pipe I/O. Never issues a blocking read."""
        buf = b""
        try:
            while not self._closed.is_set():
                did_work = False

                # 1. read whatever is available (never blocks)
                avail = _bytes_available(self._pipe)
                if avail:
                    chunk = self._pipe.read(avail)
                    if not chunk:
                        break  # pipe closed
                    buf += chunk
                    did_work = True
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self._dispatch(line)

                # 2. drain pending writes (no read is pending -> cannot deadlock)
                try:
                    while True:
                        payload = self._outbox.get_nowait()
                        self._pipe.write(payload)
                        did_work = True
                except queue.Empty:
                    pass

                if not did_work:
                    time.sleep(POLL_INTERVAL)
        except OSError as exc:
            log.warning("IPC I/O loop terminated: %s", exc)
        finally:
            self._fail_all_pending()

    def _dispatch(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            log.warning("unparseable IPC line: %r", line[:200])
            return
        if "request_id" in msg:
            with self._pending_lock:
                waiter = self._pending.pop(msg["request_id"], None)
            if waiter is not None:
                waiter["resp"] = msg
                waiter["event"].set()
        elif "event" in msg and self._event_callback is not None:
            try:
                self._event_callback(msg)
            except Exception:
                log.exception("event callback raised (event=%s)", msg.get("event"))

    def _forget(self, request_id: int) -> None:
        with self._pending_lock:
            self._pending.pop(request_id, None)

    def _fail_all_pending(self) -> None:
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for w in waiters:
            w["event"].set()  # resp stays None -> caller raises MpvDead

    def _close_pipe(self) -> None:
        if self._pipe is not None:
            try:
                self._pipe.close()
            except OSError:
                pass
            self._pipe = None

    def _kill_process(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
                self._proc.wait(2)
            except OSError:
                pass
