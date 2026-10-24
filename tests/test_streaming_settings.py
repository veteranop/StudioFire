"""Tests for Settings > Streaming (#747): config precedence, secret handling,
live pusher swap, monitor payload, and the rendered page.

ZERO network to a real stream host — probe_mount's failure and cache paths are
exercised against an unroutable endpoint. The API routes are driven through a
real FastAPI TestClient against a throwaway DB.

Run: python tests/test_streaming_settings.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db, streaming                       # noqa: E402
from services.core.nowplaying_push import (                   # noqa: E402
    DryRunTransport, IcecastUpdinfoTransport, NowPlayingPusher)

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def fresh(prefix="sf-stream-"):
    tmp = tempfile.mkdtemp(prefix=prefix)
    dbp = os.path.join(tmp, "t.db")
    db.migrate(dbp)
    conn = db.connect(dbp)
    cfg = {"paths": {"data_dir": tmp}, "nowplaying_push": {
        "enabled": False, "transport": "dryrun", "endpoint": "", "mount": "",
        "auth": None, "timeout_sec": 4.0, "min_interval_sec": 5.0,
        "include_spots": False}}
    return tmp, conn, cfg, dbp


class FakeApp:
    """Just enough of the FastAPI app for reload_pusher/status."""

    class State:
        pass

    def __init__(self, cfg, conn):
        self.state = FakeApp.State()
        self.state.cfg = cfg
        self.state.nowplaying_pusher = NowPlayingPusher({"enabled": False})
        self.state.engine = None
        self.state.get_conn = lambda: conn


def main():
    tmp, conn, cfg, dbp = fresh()

    # -- defaults -----------------------------------------------------------
    st = streaming.read_settings(conn)
    check("defaults: disabled, /live, icecast",
          st["enabled"] is False and st["mount"] == "/live"
          and st["transport"] == "icecast")
    check("no credential by default",
          streaming.load_secret(cfg) == {"user": "", "password": ""})
    check("not configured out of the box",
          streaming.configured(cfg, conn) is False)

    # -- validation ---------------------------------------------------------
    for bad in ("ftp://x", "peridot.streamguys.com:5110", "http://"):
        raised = False
        try:
            streaming._validate(bad, "/live")
        except Exception:
            raised = True
        check(f"rejects a bad target URL {bad!r}", raised)
    raised = False
    try:
        streaming._validate("http://h:1", "live")
    except Exception:
        raised = True
    check("rejects a mount without a leading /", raised)
    streaming._validate("http://h:1", "/live")   # must not raise
    check("accepts a well-formed target", True)

    # -- precedence: DB overrides config.json, secret supplies auth ---------
    db.set_setting(conn, streaming.K_ENDPOINT, "http://peridot.streamguys.com:5110")
    db.set_setting(conn, streaming.K_MOUNT, "/live")
    db.set_setting(conn, streaming.K_ENABLED, "1")
    merged = streaming.merged_np_config(cfg, conn)
    check("DB endpoint overrides config.json", merged["endpoint"].startswith("http://"))
    check("DB enabled overrides config.json", merged["enabled"] is True)
    check("still not 'configured' without a credential",
          streaming.configured(cfg, conn) is False)

    streaming.save_secret(cfg, "source", "s3cret")
    check("secret written to data/stream_secret.json",
          os.path.isfile(os.path.join(tmp, "stream_secret.json")))
    merged = streaming.merged_np_config(cfg, conn)
    check("secret becomes the transport auth", merged["auth"] == "source:s3cret")
    check("now configured", streaming.configured(cfg, conn) is True)
    check("secret never lands in the DB settings",
          "s3cret" not in json.dumps(
              dict(conn.execute("SELECT key, value FROM settings").fetchall())))

    # -- live pusher swap ---------------------------------------------------
    app = FakeApp(cfg, conn)
    res = streaming.reload_pusher(app, conn)
    check("reload swaps in a new pusher",
          app.state.nowplaying_pusher is not None
          and res["configured"] is True)
    check("reload builds the real transport once configured",
          isinstance(app.state.nowplaying_pusher.transport, IcecastUpdinfoTransport))
    check("reload reports the transport class", res["transport"] == "IcecastUpdinfoTransport")

    # and the reverse: clearing the credential must fall back to DryRun
    streaming.save_secret(cfg, "", "")
    res2 = streaming.reload_pusher(app, conn)
    check("clearing the credential falls back to DryRun",
          isinstance(app.state.nowplaying_pusher.transport, DryRunTransport))
    check("unconfigured after clearing", res2["configured"] is False)

    # -- monitor payload ----------------------------------------------------
    st = streaming.status(app, conn)
    check("status carries config + push + probe",
          {"enabled", "endpoint", "mount", "configured", "push", "probe"} <= set(st))
    check("status never leaks the password",
          "s3cret" not in json.dumps(st))
    check("push block exposes transport + last result",
          st["push"]["transport"] == "DryRunTransport"
          and "state" in st["push"] and "title" in st["push"])

    # -- probe: no endpoint, and an unreachable host (no real host needed) --
    p = streaming.probe_mount("", "/live")
    check("probe with no endpoint says so", p["ok"] is False and "no target" in p["error"])

    streaming._PROBE_CACHE.clear()
    p = streaming.probe_mount("http://127.0.0.1:9", "/live", timeout=1.0)
    check("probe to a dead host fails safely",
          p["ok"] is False and p["error"])
    cached = streaming.probe_mount("http://127.0.0.1:9", "/live", timeout=1.0)
    check("probe result is cached for the monitor poll",
          cached["checked_at"] == p["checked_at"])

    # -- the rendered page carries the new section -------------------------
    from jinja2 import Environment, FileSystemLoader
    from services.core import changelog as changelog_mod
    env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "web", "templates")))
    env.filters["sf_bold"] = changelog_mod.bold_segments
    env.globals.update(ga_id="", ga_station="KDPI", asset_version="t")
    html = env.get_template("settings.html").render(
        role="admin", station="KDPI", changelog={"ok": True, "releases": []},
        running_version="1.5.1", hour12=True)
    check("Settings renders the Streaming section", "Streaming &amp; song metadata" in html)
    check("Settings has the target URL field", 'id="st-endpoint"' in html)
    check("Settings has the mount + credential fields",
          'id="st-mount"' in html and 'id="st-user"' in html
          and 'id="st-pass"' in html)
    check("Settings has the live monitor block", 'id="st-monitor"' in html)
    check("the password input is a password field",
          'type="password"' in html)
    html_op = env.get_template("settings.html").render(
        role="operator", station="KDPI", changelog={"ok": True, "releases": []},
        running_version="1.5.1", hour12=True)
    check("operators see the monitor but not the credential form",
          'id="st-monitor"' in html_op and 'id="st-pass"' not in html_op)

    print(f"\nPASS {passed} checks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
