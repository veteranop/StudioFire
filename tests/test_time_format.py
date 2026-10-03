"""Clock display format — the Settings toggle between 12-hour (AM/PM) and
24-hour (military) time (John's request), and the server-side labels that must
follow it.

Run: python tests/test_time_format.py
"""
import datetime
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient              # noqa: E402

from services.core import schedule as sched          # noqa: E402
from services.core.app import create_app              # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def main():
    # ---- pure formatting helpers honour the module flag
    sched.set_hour12(True)
    check("12h: _fmt_tod evening", sched._fmt_tod("18:30") == "6:30 PM")
    check("12h: _fmt_tod midnight", sched._fmt_tod("00:05") == "12:05 AM")
    check("12h: _fmt_tod noon", sched._fmt_tod("12:00") == "12:00 PM")
    check("12h: _fmt_tod morning (no leading zero)",
          sched._fmt_tod("06:00") == "6:00 AM")
    sched.set_hour12(False)
    check("24h: _fmt_tod evening", sched._fmt_tod("18:30") == "18:30")
    check("24h: _fmt_tod morning zero-padded", sched._fmt_tod("06:00") == "06:00")
    check("24h: _fmt_dt once-show timestamp",
          sched._fmt_dt("2026-10-03T18:30") == "2026-10-03 18:30")
    sched.set_hour12(True)
    check("12h: _fmt_dt once-show timestamp",
          sched._fmt_dt("2026-10-03T18:30") == "2026-10-03 6:30 PM")

    when = datetime.datetime(2026, 10, 4, 6, 0)
    now = datetime.datetime(2026, 10, 3, 12, 0)
    check("12h: next_label", sched.next_label(when, now) == "Tomorrow 6:00 AM")
    sched.set_hour12(False)
    check("24h: next_label", sched.next_label(when, now) == "Tomorrow 06:00")

    # ---- the setting round-trips through the app and drives the templates
    td = tempfile.mkdtemp(prefix="sf-tf-")
    cfg = {"station_name": "TestFM", "db_path": os.path.join(td, "core.db"),
           "secret_path": os.path.join(td, "secret.key"),
           "precache_dir": os.path.join(td, "precache"), "data_dir": td,
           "nas_music_root": td, "engine_url": "http://127.0.0.1:1",
           "journal_path": os.path.join(td, "j.jsonl"),
           "precache_target_minutes": 1, "feeder_enabled": False}
    app = create_app(cfg)          # startup applies the saved format (default 12)
    client = TestClient(app, follow_redirects=False)
    client.post("/setup", data={"username": "op", "password": "longenough",
                                "password2": "longenough"})

    check("default format is 12-hour",
          client.get("/api/settings/time_format").json()["format"] == "12")
    check("dashboard renders the 12h flag for the header clock",
          b"window.SF_HOUR12 = true" in client.get("/").content)
    check("settings page shows 12-hour selected",
          b'value="12" selected' in client.get("/settings").content)

    r = client.post("/api/settings/time_format", json={"format": "24"})
    check("POST 24-hour accepted",
          r.status_code == 200 and r.json()["format"] == "24")
    check("schedule module flag flipped live", sched.hour12() is False)
    check("GET now returns 24-hour",
          client.get("/api/settings/time_format").json()["format"] == "24")
    check("dashboard renders the 24h flag",
          b"window.SF_HOUR12 = false" in client.get("/").content)
    check("settings page shows 24-hour selected",
          b'value="24" selected' in client.get("/settings").content)

    check("garbage format rejected (400)",
          client.post("/api/settings/time_format",
                      json={"format": "yes"}).status_code == 400)
    check("format endpoint needs auth (401)",
          TestClient(app, follow_redirects=False)
          .get("/api/settings/time_format").status_code == 401)

    # ---- the setting survives a restart (persisted in the DB)
    create_app(cfg)                # startup re-applies the saved format
    check("24-hour choice persists across a restart", sched.hour12() is False)

    print("\nTIME FORMAT OK (%d checks)" % passed)


if __name__ == "__main__":
    main()
