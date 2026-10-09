"""Unit tests for the now-playing pusher (#747) — ZERO network.

Covers: config default-off + env-sourced secret, transport factory gating,
enqueue-only filtering (title None / spots / dedup / throttle), the durable
last-pushed journal_id marker (no history replay on restart), backlog
reconcile to the current track, and the worker thread actually draining the
bounded queue to a fake transport without ever raising.

Run: python tests/test_nowplaying_push.py
"""
import os
import queue
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db                                   # noqa: E402
from services.core.config import load_config                   # noqa: E402
from services.core.nowplaying_push import (                    # noqa: E402
    DryRunTransport, NowPlayingPusher, Transport, build_transport)

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def fresh_conn():
    tmp = tempfile.mkdtemp(prefix="sf-np-")
    dbp = os.path.join(tmp, "t.db")
    db.migrate(dbp)
    return db.connect(dbp)


def ev(ms, seq=1, title="Artist - Song", source="playlist", path=None):
    return {"id": f"{ms}-{seq}", "event": "track_start", "title": title,
            "source": source, "path": path or f"/music/{ms}.mp3"}


class FakeTransport(Transport):
    """Records every payload on a thread-safe queue — no I/O, no network."""

    def __init__(self):
        self.received: queue.Queue = queue.Queue()

    def send(self, payload, timeout):
        self.received.put(payload)


class RaisingTransport(Transport):
    def __init__(self):
        self.calls = queue.Queue()

    def send(self, payload, timeout):
        self.calls.put(payload)
        raise RuntimeError("boom")


def pusher(enabled=True, transport=None, **kw):
    cfg = {"enabled": enabled, "transport": "dryrun", "timeout_sec": 1.0,
           "min_interval_sec": 0.0, "include_spots": False}
    cfg.update(kw)
    return NowPlayingPusher(cfg, transport=transport)


# --------------------------------------------------------------- config

def test_config_defaults_off_and_env_secret():
    os.environ.pop("STUDIOFIRE_NOWPLAYING_AUTH", None)
    np = load_config("/does/not/exist.json")["nowplaying_push"]
    check("config: enabled defaults False", np["enabled"] is False)
    check("config: transport defaults dryrun", np["transport"] == "dryrun")
    check("config: include_spots defaults False", np["include_spots"] is False)
    check("config: auth None when env unset", np["auth"] is None)
    os.environ["STUDIOFIRE_NOWPLAYING_AUTH"] = "shh"
    np2 = load_config("/does/not/exist.json")["nowplaying_push"]
    check("config: auth is env-sourced", np2["auth"] == "shh")
    os.environ.pop("STUDIOFIRE_NOWPLAYING_AUTH", None)


def test_config_ignores_committed_auth():
    # even if a secret is (wrongly) dropped into config, it is NOT read
    cfg = {"nowplaying_push": {"auth": "committed-leak", "auth_env":
                               "STUDIOFIRE_NOWPLAYING_AUTH"}}
    import json as _json
    tmp = tempfile.mkdtemp(prefix="sf-np-cfg-")
    p = os.path.join(tmp, "config.json")
    with open(p, "w") as f:
        _json.dump(cfg, f)
    os.environ.pop("STUDIOFIRE_NOWPLAYING_AUTH", None)
    np = load_config(p)["nowplaying_push"]
    check("config: committed auth ignored (env-only)", np["auth"] is None)


# -------------------------------------------------------------- transport

def test_transport_factory_gated():
    check("factory: dryrun -> DryRunTransport",
          isinstance(build_transport({"transport": "dryrun"}), DryRunTransport))
    check("factory: ungated transport falls back to DryRun",
          isinstance(build_transport({"transport": "icecast_updinfo"}),
                     DryRunTransport))
    # DryRun does no I/O and never raises
    DryRunTransport().send({"song": "x"}, 1.0)
    check("dryrun: send is a no-op", True)


# ----------------------------------------------------------- flag-off

def test_disabled_does_nothing():
    conn = fresh_conn()
    ft = FakeTransport()
    p = pusher(enabled=False, transport=ft)
    p.start()
    check("disabled: no worker thread", p._thread is None)
    n = p.on_ingested(conn, [ev(1000, title="A", path="/a.mp3")],
                      now_playing="/a.mp3")
    check("disabled: on_ingested returns 0", n == 0)
    check("disabled: nothing enqueued", p._q.qsize() == 0)
    check("disabled: marker untouched",
          db.get_setting(conn, "nowplaying_push") is None)


# --------------------------------------------------------- enqueue filters

def test_basic_enqueue_and_payload():
    conn = fresh_conn()
    p = pusher()
    n = p.on_ingested(conn, [ev(1000, title="A - B", path="/a.mp3")],
                      now_playing="/a.mp3")
    check("enqueue: one track enqueued", n == 1 and p._q.qsize() == 1)
    payload = p._q.get_nowait()
    check("enqueue: payload song is journal title", payload["song"] == "A - B")
    check("enqueue: payload carries journal_id", payload["journal_id"] == "1000-1")


def test_skip_title_none():
    conn = fresh_conn()
    p = pusher()
    n = p.on_ingested(conn, [ev(1000, title=None, source="emergency")])
    check("filter: title None (filler) skipped", n == 0 and p._q.qsize() == 0)


def test_skip_spots_unless_included():
    conn = fresh_conn()
    p = pusher()
    n = p.on_ingested(conn, [ev(1000, title="AD", source="spot")])
    check("filter: spot skipped by default", n == 0)
    conn2 = fresh_conn()
    p2 = pusher(include_spots=True)
    n2 = p2.on_ingested(conn2, [ev(1000, title="AD", source="spot")])
    check("filter: spot pushed when include_spots", n2 == 1)


def test_dedup_consecutive_identical():
    conn = fresh_conn()
    p = pusher()
    n1 = p.on_ingested(conn, [ev(1000, title="SAME", path="/1.mp3")])
    n2 = p.on_ingested(conn, [ev(1001, title="SAME", path="/2.mp3")])
    check("dedup: first identical pushed", n1 == 1)
    check("dedup: second identical skipped", n2 == 0)
    n3 = p.on_ingested(conn, [ev(1002, title="DIFFERENT", path="/3.mp3")])
    check("dedup: changed title pushed again", n3 == 1)


def test_min_interval_throttle():
    conn = fresh_conn()
    p = pusher(min_interval_sec=100.0)
    n1 = p.on_ingested(conn, [ev(1000, title="A", path="/1.mp3")])
    n2 = p.on_ingested(conn, [ev(1001, title="B", path="/2.mp3")])
    check("throttle: first pushed", n1 == 1)
    check("throttle: second within window dropped", n2 == 0)


# ------------------------------------------------- marker / no-replay

def test_marker_persists_and_blocks_replay():
    conn = fresh_conn()
    p = pusher()
    p.on_ingested(conn, [ev(2000, title="A", path="/a.mp3")], now_playing="/a.mp3")
    marker = db.get_setting(conn, "nowplaying_push")
    check("marker: persisted", marker is not None and "2000-1" in marker)
    # a fresh pusher (simulating a P2 restart) must NOT replay the same id
    p2 = pusher()
    n = p2.on_ingested(conn, [ev(2000, title="A", path="/a.mp3")])
    check("marker: restart does not replay seen id", n == 0)
    n_old = p2.on_ingested(conn, [ev(1999, title="OLD", path="/o.mp3")])
    check("marker: older-than-marker id ignored", n_old == 0)
    n_new = p2.on_ingested(conn, [ev(2001, title="NEW", path="/n.mp3")])
    check("marker: newer id still pushes", n_new == 1)


def test_backlog_reconciles_to_current():
    conn = fresh_conn()
    p = pusher()
    batch = [ev(3000, title="A", path="/a.mp3"),
             ev(3001, title="B", path="/b.mp3"),
             ev(3002, title="C", path="/c.mp3")]
    n = p.on_ingested(conn, batch, now_playing="/b.mp3")
    check("backlog: exactly one enqueued", n == 1 and p._q.qsize() == 1)
    payload = p._q.get_nowait()
    check("backlog: enqueued the CURRENT (now_playing) track",
          payload["song"] == "B")
    marker = db.get_setting(conn, "nowplaying_push")
    check("backlog: marker advanced past the whole batch", "3002-1" in marker)


def test_backlog_without_status_takes_newest():
    conn = fresh_conn()
    p = pusher()
    batch = [ev(4000, title="A", path="/a.mp3"),
             ev(4002, title="C", path="/c.mp3"),
             ev(4001, title="B", path="/b.mp3")]
    n = p.on_ingested(conn, batch, now_playing=None)
    check("backlog(no status): one enqueued", n == 1)
    check("backlog(no status): newest wins", p._q.get_nowait()["song"] == "C")


# ------------------------------------------------- worker thread (drain)

def test_worker_drains_to_transport():
    conn = fresh_conn()
    ft = FakeTransport()
    p = pusher(transport=ft)
    p.start()
    try:
        p.on_ingested(conn, [ev(5000, title="LIVE - SONG", path="/l.mp3")],
                      now_playing="/l.mp3")
        payload = ft.received.get(timeout=3.0)
        check("worker: transport received the update",
              payload["song"] == "LIVE - SONG")
    finally:
        p.stop()
    check("worker: thread stopped cleanly",
          p._thread is None or not p._thread.is_alive())


def test_worker_never_raises_on_transport_error():
    conn = fresh_conn()
    rt = RaisingTransport()
    p = pusher(transport=rt)
    p.start()
    try:
        p.on_ingested(conn, [ev(6000, title="X", path="/x.mp3")],
                      now_playing="/x.mp3")
        rt.calls.get(timeout=3.0)  # worker attempted the send (and it raised)
        time.sleep(0.2)
        check("never-raises: worker survives a raising transport",
              p._thread.is_alive())
        # and it keeps draining afterwards
        p.on_ingested(conn, [ev(6001, title="Y", path="/y.mp3")],
                      now_playing="/y.mp3")
        rt.calls.get(timeout=3.0)
        check("never-raises: worker keeps draining after an error", True)
    finally:
        p.stop()


def test_drop_on_full():
    # tiny bound, worker NOT started → queue fills and extra puts are dropped
    # silently (no exception, never blocks the feeder)
    p = NowPlayingPusher({"enabled": True, "transport": "dryrun"},
                         transport=DryRunTransport(), queue_max=1)
    p._enqueue({"song": "1"})
    p._enqueue({"song": "2"})  # dropped
    p._enqueue({"song": "3"})  # dropped
    check("drop-on-full: queue bounded at max", p._q.qsize() == 1)
    check("drop-on-full: no exception raised", True)


if __name__ == "__main__":
    test_config_defaults_off_and_env_secret()
    test_config_ignores_committed_auth()
    test_transport_factory_gated()
    test_disabled_does_nothing()
    test_basic_enqueue_and_payload()
    test_skip_title_none()
    test_skip_spots_unless_included()
    test_dedup_consecutive_identical()
    test_min_interval_throttle()
    test_marker_persists_and_blocks_replay()
    test_backlog_reconciles_to_current()
    test_backlog_without_status_takes_newest()
    test_worker_drains_to_transport()
    test_worker_never_raises_on_transport_error()
    test_drop_on_full()
    print(f"\nPASS — {passed} checks")
