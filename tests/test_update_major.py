"""§6.3 major-version gate — the UI/endpoint side, DEFENSIVE ONLY.

SCOPE (Mark, 2026-10-05): this 1.x release must DETECT a higher major, INFORM
an admin, and NEVER expose a way to install one. So this proves:
  * /api/update exposes `major_available` (data only) + `can_major` (admin-only
    visibility of the informational note); operators get can_major False;
  * there is NO install path for a major: POST /api/update/install-major and
    GET /api/update/major-preflight simply do not exist (404) — for admin,
    operator, and signed-out alike, so it can't be triggered by anyone;
  * the everyday /api/update/install only ever targets the within-major
    release, so it can't cross a major even when one is available.

The full admin major-install experience (typed confirm, pre-flight, the
install-major endpoint) is DEFERRED to a later release, after 2.0 is proven on
Mark's home PC with a 72-hour burn-in.

Run: python -m tests.test_update_major
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient          # noqa: E402

from services import updater                        # noqa: E402
from services.core.app import create_app            # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


# a check() result reporting a within-major update (1.6.0) AND a higher major
# (2.0.0) at once — the gate must keep them apart on the UI side.
FAKE_CHECK = {
    "current": "1.5.0", "checked_at": 0.0, "dev_checkout": False,
    "error": None,
    "latest": {"version": "1.6.0", "tag": "v1.6.0", "name": "StudioFire v1.6.0",
               "notes": "within", "zip_url": "x", "html_url": "x",
               "published_at": "x"},
    "update_available": True,
    "major_available": {"version": "2.0.0", "tag": "v2.0.0",
                        "notes": "the big one", "html_url": "x"},
}

spawned = []        # records any spawn_detached_apply(...) call


def fake_spawn(tag=None, major=False):
    spawned.append((tag, major))


def main():
    td = tempfile.mkdtemp(prefix="sf-major-")
    cfg = {"station_name": "TestFM",
           "db_path": os.path.join(td, "core.db"),
           "secret_path": os.path.join(td, "secret.key"),
           "precache_dir": os.path.join(td, "precache"),
           "data_dir": td,
           "nas_music_root": td,
           "engine_url": "http://127.0.0.1:1",     # unreachable -> no emergency
           "journal_path": os.path.join(td, "play_journal.jsonl"),
           "feeder_enabled": False}
    app = create_app(cfg)

    updater.is_dev_checkout = lambda root=None: False
    updater.check = lambda: dict(FAKE_CHECK)
    updater.spawn_detached_apply = fake_spawn

    # the build ships with major install disabled — the plumbing exists for a
    # later release, but nothing in this build drives it
    check("build ships with major install disabled",
          updater.MAJOR_INSTALL_ENABLED is False)

    admin = TestClient(app, follow_redirects=False)
    admin.post("/setup", data={"username": "boss", "password": "longenough",
                               "password2": "longenough"})
    # populate the server's cached check() so major_available is known
    v = admin.post("/api/update/check").json()

    # ---- the view carries the major as DATA + admin visibility, nothing more
    check("update view exposes major_available (data only)",
          v["major_available"] and v["major_available"]["version"] == "2.0.0"
          and v["major_available"]["tag"] == "v2.0.0")
    check("admin can_major is True (the note is admin-only)",
          v["can_major"] is True)
    check("the everyday button still points WITHIN this major (1.6.0)",
          v["update_available"] and v["latest"]["version"] == "1.6.0")
    check("no typed-confirm / install plumbing leaks into the view",
          "major_version" not in v and "major_install_enabled" not in v)

    # ---- THE HEART OF THIS RELEASE: no install path for a major exists.
    spawned.clear()
    r = admin.post("/api/update/install-major", json={"confirm_version": "2.0"})
    check("no install-major endpoint exists, even for admin (404)",
          r.status_code == 404 and not spawned)
    r = admin.get("/api/update/major-preflight")
    check("no major-preflight endpoint exists, even for admin (404)",
          r.status_code == 404)

    # ---- an operator sees no major note, and has no path either
    admin.post("/api/users", json={"username": "dj", "password": "longenough",
                                   "role": "Basic"})
    op = TestClient(app, follow_redirects=False)
    op.post("/login", data={"username": "dj", "password": "longenough"})
    vo = op.get("/api/update").json()
    check("operators never get can_major (they see no major note)",
          vo["can_major"] is False
          and vo["major_available"]["version"] == "2.0.0")
    r = op.post("/api/update/install-major", json={"confirm_version": "2.0"})
    check("operator also hits 404 (there is simply no such path); nothing spawned",
          r.status_code == 404 and not spawned)

    # ---- signed-out: still no path (routing 404s before any auth runs)
    anon = TestClient(app, follow_redirects=False)
    r = anon.post("/api/update/install-major", json={"confirm_version": "2.0"})
    check("signed-out hits 404 too; nothing spawned",
          r.status_code == 404 and not spawned)

    # ---- the everyday install can never be steered across a major
    spawned.clear()
    r = admin.post("/api/update/install")
    check("the everyday install targets the within-major release (1.6.0)",
          r.status_code == 200 and r.json()["to"] == "1.6.0")
    check("it spawned a normal (non-major) apply for the within-major tag",
          spawned == [("v1.6.0", False)])

    print(f"UPDATE MAJOR OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
