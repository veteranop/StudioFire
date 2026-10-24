"""Tests for the Icecast `updinfo` now-playing transport (#747).

A real HTTP server on 127.0.0.1 plays the part of the StreamGuys mount, so the
request shape is exercised end to end without touching a production CDN.

Covers:
  * URL/mount/song encoding (spaces, ampersands, non-ASCII)
  * HTTP Basic auth actually sent
  * a 401 from the mount raises (so the pusher's worker swallows it and the
    title is simply lost — never a strike at the audio path)
  * the factory refusing to build a live transport without endpoint/mount/auth
  * the worker thread surviving a failing transport

Run: python tests/test_icecast_transport.py
"""
import base64
import http.server
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core.nowplaying_push import (                      # noqa: E402
    DryRunTransport, IcecastUpdinfoTransport, NowPlayingPusher, build_transport)

passed = 0
SEEN = []


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


class FakeMount(http.server.BaseHTTPRequestHandler):
    want_auth = "Basic " + base64.b64encode(b"source:s3cret").decode()

    def do_GET(self):  # noqa: N802
        SEEN.append({"path": self.path, "auth": self.headers.get("Authorization")})
        if self.headers.get("Authorization") != self.want_auth:
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Metadata update successful")

    def log_message(self, *a):
        pass


def start_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), FakeMount)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def main():
    srv, base = start_server()
    tr = IcecastUpdinfoTransport(base, "/live", "source:s3cret")

    url = tr.build_url("Bob Dylan - Like a Rolling Stone")
    check("url hits the icecast admin metadata endpoint",
          url.startswith(base + "/admin/metadata?"))
    check("url carries mount + updinfo mode",
          "mount=%2Flive" in url and "mode=updinfo" in url)
    check("url percent-encodes the song",
          "song=Bob+Dylan+-+Like+a+Rolling+Stone" in url)

    tr.send({"song": "Echo & The Bunnymen - Seven Seas"}, 3.0)
    sent = SEEN[-1]
    check("request reached the mount", sent["path"].startswith("/admin/metadata?"))
    check("basic auth sent", sent["auth"] == FakeMount.want_auth)
    check("ampersand survived encoding", "Seven+Seas" in sent["path"]
          and "Echo+%26+The+Bunnymen" in sent["path"])

    tr.send({"song": "Björk – Jóga"}, 3.0)
    check("non-ASCII is percent-encoded, not mangled",
          "Bj%C3%B6rk" in SEEN[-1]["path"])

    # empty song => no request at all (an ID with no title must not blank the
    # player for the listener)
    before = len(SEEN)
    tr.send({"song": ""}, 3.0)
    check("empty title sends nothing", len(SEEN) == before)

    # a wrong credential must raise (worker swallows it)
    bad = IcecastUpdinfoTransport(base, "/live", "source:wrong")
    raised = False
    try:
        bad.send({"song": "X - Y"}, 3.0)
    except Exception:
        raised = True
    check("401 from the mount raises", raised)

    # unconfigured transport raises rather than silently no-op'ing
    raised = False
    try:
        IcecastUpdinfoTransport(base, "/live", "").send({"song": "X - Y"}, 3.0)
    except Exception:
        raised = True
    check("missing credential raises", raised)

    # factory gating
    check("factory falls back to DryRun when auth is absent",
          isinstance(build_transport({"transport": "icecast",
                                      "endpoint": base, "mount": "/live",
                                      "auth": None}), DryRunTransport))
    check("factory builds the live transport when fully configured",
          isinstance(build_transport({"transport": "icecast", "endpoint": base,
                                      "mount": "/live",
                                      "auth": "source:s3cret"}),
                     IcecastUpdinfoTransport))
    check("factory still defaults to DryRun",
          isinstance(build_transport({}), DryRunTransport))

    # the pusher's worker must survive a failing transport and keep running
    class Boom(DryRunTransport):
        calls = 0

        def send(self, payload, timeout):
            Boom.calls += 1
            raise RuntimeError("mount exploded")

    pusher = NowPlayingPusher({"enabled": True, "min_interval_sec": 0},
                              transport=Boom(), queue_max=8)
    pusher.start()
    pusher._enqueue({"song": "A - B"})
    time.sleep(0.6)
    alive = pusher._thread.is_alive()
    pusher.stop()
    check("worker survived a failing transport", alive and Boom.calls == 1)

    srv.shutdown()
    print(f"\nPASS {passed} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
