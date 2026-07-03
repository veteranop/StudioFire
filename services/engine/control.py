"""Localhost-only HTTP control surface for P1. PLAN.md §5/§10.2.

P1 design laws apply: STDLIB ONLY (http.server). Binds 127.0.0.1 ONLY —
P2 runs on the same machine; nothing on the LAN talks to P1 directly.

Endpoints (JSON in/out):
    GET  /status          -> supervisor status snapshot
    GET  /health          -> {"ok": true}  (liveness for NSSM/P2)
    POST /queue           -> queue mutation {op, queue_version, entries?}
                             202 accepted / 409 stale version / 400 bad op
    POST /op              -> {"op": "skip"|"pause"|"resume"}

Every request is handled in its own thread (ThreadingHTTPServer) and every
supervisor call has a timeout — a stuck request can never wedge playback
(the supervisor owner thread never blocks on us; we block on it, briefly).
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("engine.control")

MAX_BODY = 5 * 1024 * 1024  # a very large queue is ~1MB; 5MB is generous


class ControlServer:
    def __init__(self, supervisor, host: str = "127.0.0.1", port: int = 7701):
        if host != "127.0.0.1":
            raise ValueError("P1 control surface must bind 127.0.0.1 only")
        self._supervisor = supervisor
        handler = _make_handler(supervisor)
        self._httpd = ThreadingHTTPServer((host, port), handler)
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="engine-control", daemon=True)
        self._thread.start()
        log.info("control surface on 127.0.0.1:%d", self.port)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(2)


def _make_handler(supervisor):
    class Handler(BaseHTTPRequestHandler):
        server_version = "StudioFireEngine"

        def log_message(self, fmt, *args):  # route to logging, not stderr
            log.debug("%s " + fmt, self.address_string(), *args)

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > MAX_BODY:
                return None
            try:
                return json.loads(self.rfile.read(length))
            except ValueError:
                return None

        def do_GET(self):
            if self.path == "/status":
                self._send(200, supervisor.status())
            elif self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            body = self._read_json()
            if body is None:
                self._send(400, {"error": "invalid or missing JSON body"})
                return
            if self.path == "/queue":
                ok, why = supervisor.submit_mutation(body)
                if ok:
                    self._send(202, {"accepted": True,
                                     "queue_version": body.get("queue_version")})
                elif "stale" in why:
                    self._send(409, {"accepted": False, "reason": why,
                                     "status": supervisor.status()})
                else:
                    self._send(400, {"accepted": False, "reason": why})
            elif self.path == "/op":
                ok, why = supervisor.submit_command(str(body.get("op")))
                self._send(200 if ok else 400, {"accepted": ok, "reason": why})
            else:
                self._send(404, {"error": "not found"})

    return Handler
