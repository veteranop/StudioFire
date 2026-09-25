"""Self-updater tests (services/updater.py) against a fake GitHub.

A temp "installed station" is built from this repo's shipped files, and a
local HTTP server plays GitHub's releases API + zipball downloads. Service
restarts and health checks are faked (recorded / scripted) so this never
touches a running StudioFire; everything else — download, verification,
backup, file swap, mirror-deletes, rollback — runs for real.

Run: python -m tests.test_updater
"""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services import updater  # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


# ------------------------------------------------------------ fake GitHub

RELEASES: dict[str, dict] = {}     # tag -> {"zip": bytes, "notes": str}
LATEST = {"tag": None}


class FakeGitHub(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        port = self.server.server_address[1]
        if self.path.startswith("/zip/"):
            tag = self.path[5:]
            return self._send(200, RELEASES[tag]["zip"], "application/zip")
        if self.path == "/releases/latest":
            tag = LATEST["tag"]
        elif self.path.startswith("/releases/tags/"):
            tag = self.path[len("/releases/tags/"):]
        else:
            tag = None
        if tag not in RELEASES:
            return self._send(404, {"message": "Not Found"})
        return self._send(200, {
            "tag_name": tag, "name": f"StudioFire {tag}",
            "body": RELEASES[tag]["notes"],
            "zipball_url": f"http://127.0.0.1:{port}/zip/{tag}",
            "html_url": "https://example.invalid", "published_at": "x"})

    def log_message(self, *a):
        pass


def make_release(tag, version=None, edits=None, drop=None, notes="notes"):
    """Zip of the repo's shipped files as GitHub would serve them (one
    '<owner>-<repo>-<sha>/' folder). `edits`: {relpath: text}; `drop`: files
    to leave out (i.e. deleted in this release)."""
    buf = io.BytesIO()
    top = "veteranop-StudioFire-abc1234/"
    def key(rel):
        return rel.replace("\\", "/")
    files = {key(rel): open(os.path.join(ROOT, rel), "rb").read()
             for rel in updater.managed_files(ROOT)}
    files["VERSION"] = ((version or tag.lstrip("v")) + "\n").encode()
    for rel, text in (edits or {}).items():
        files[key(rel)] = text.encode()
    for rel in (drop or []):
        files.pop(key(rel), None)
    with zipfile.ZipFile(buf, "w") as z:
        for rel, data in files.items():
            z.writestr(top + rel.replace("\\", "/"), data)
    RELEASES[tag] = {"zip": buf.getvalue(), "notes": notes}


# ---------------------------------------------------------- fake station

def make_station(version="1.0.0"):
    st = tempfile.mkdtemp(prefix="sf-station-")
    for rel in updater.managed_files(ROOT):
        dst = os.path.join(st, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, rel), dst)
    with open(os.path.join(st, "VERSION"), "w") as f:
        f.write(version + "\n")
    os.makedirs(os.path.join(st, "data"))
    with open(os.path.join(st, "config", "config.json"), "w") as f:
        f.write('{"station_name": "KEEP ME"}')
    db = sqlite3.connect(os.path.join(st, "data", "studiofire.db"))
    db.execute("CREATE TABLE t (x)")
    db.execute("INSERT INTO t VALUES (42)")
    db.commit()
    db.close()
    # a stale module that a newer release no longer ships
    with open(os.path.join(st, "services", "core", "old_module.py"), "w") as f:
        f.write("X = 1\n")
    return st


restarts: list[list[str]] = []
health = {"results": []}      # scripted wait_healthy results, popped in order


def fake_restart(names):
    restarts.append(list(names))


def fake_wait_healthy(names, version, timeout=0):
    return health["results"].pop(0) if health["results"] else (True, "ok")


def use_station(st):
    updater.ROOT = st
    restarts.clear()
    health["results"] = []


def read(st, rel):
    with open(os.path.join(st, rel), encoding="utf-8") as f:
        return f.read()


def main():
    srv = HTTPServer(("127.0.0.1", 0), FakeGitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    updater.API_BASE = f"http://127.0.0.1:{srv.server_address[1]}"
    updater.restart = fake_restart
    updater.wait_healthy = fake_wait_healthy

    # ---- pure helpers
    check("version parse", updater.parse_version("v1.10.2") == (1, 10, 2)
          and updater.parse_version("1.2") == (1, 2, 0))
    check("engine code change restarts the engine (+ nothing else)",
          updater.services_to_restart(["services\\engine\\supervisor.py"])
          == ["engine"])
    check("web-only change never restarts the engine (no audio drop)",
          updater.services_to_restart(["web\\templates\\dashboard.html",
                                       "services\\core\\app.py"])
          == ["core", "worker"])
    check("docs-only change still restarts web (keeps VERSION honest)",
          updater.services_to_restart(["CHANGELOG.md"]) == ["core", "worker"])

    # ---- check()
    st = make_station("1.0.0")
    use_station(st)
    make_release("v1.1.0", notes="Songs fade into each other.")
    LATEST["tag"] = "v1.1.0"
    c = updater.check()
    check("check: newer release offered with its notes",
          c["update_available"] and c["latest"]["version"] == "1.1.0"
          and "fade" in c["latest"]["notes"] and c["error"] is None)
    LATEST["tag"] = "nope"
    check("check: offline/no release is reported, never raised",
          updater.check()["error"] is not None)

    # ---- a good update: web-only change
    LATEST["tag"] = "v1.1.0"
    make_release("v1.1.0", edits={
        "web/templates/base.html": read(st, "web/templates/base.html")
        + "<!-- v1.1.0 -->"}, drop=[])
    rc = updater.apply()
    s = updater.read_state()
    check("apply: succeeds", rc == 0 and s["state"] == "done")
    check("apply: VERSION is the new one", read(st, "VERSION").strip()
          == "1.1.0")
    check("apply: changed file installed",
          read(st, "web/templates/base.html").endswith("<!-- v1.1.0 -->"))
    check("apply: only web + indexer restarted (audio untouched)",
          restarts == [["core", "worker"]])
    check("apply: a module the release dropped is removed",
          not os.path.exists(os.path.join(st, "services", "core",
                                          "old_module.py")))
    check("apply: config.json untouched", read(st, "config/config.json")
          == '{"station_name": "KEEP ME"}')
    bdir = s["backup_dir"]
    check("apply: backup holds the old code + a DB copy",
          read(bdir, "code/VERSION").strip() == "1.0.0"
          and os.path.exists(os.path.join(bdir, "code", "services", "core",
                                          "old_module.py"))
          and sqlite3.connect(os.path.join(bdir, "studiofire.db"))
          .execute("SELECT x FROM t").fetchone()[0] == 42)
    check("apply: live DB untouched", sqlite3.connect(
          os.path.join(st, "data", "studiofire.db"))
          .execute("SELECT x FROM t").fetchone()[0] == 42)
    check("apply: lock released", not os.path.exists(
          os.path.join(st, "data", "update.lock")))
    restarts.clear()
    check("apply again: already up to date, nothing restarted",
          updater.apply() == 0 and restarts == []
          and "up to date" in updater.read_state()["message"])

    # ---- releases rejected BEFORE anything changes
    def rejected(tag, why_part, **kw):
        make_release(tag, **kw)
        LATEST["tag"] = tag
        before = updater.managed_files(st)
        snap = {r: read(st, r) for r in ("VERSION", "services/core/app.py")}
        rc = updater.apply()
        s = updater.read_state()
        return (rc == 1 and s["state"] == "failed"
                and why_part in s["message"]
                and updater.managed_files(st) == before
                and all(read(st, r) == t for r, t in snap.items())
                and restarts == [])

    restarts.clear()
    check("reject: broken Python in the release",
          rejected("v1.2.0", "broken code",
                   edits={"services/core/app.py": "def oops(:\n"}))
    check("reject: VERSION inside doesn't match the tag",
          rejected("v1.3.0", "published wrong", version="1.2.9"))
    check("reject: needs new Python packages (full installer instead)",
          rejected("v1.4.0", "new Python packages",
                   edits={"requirements.txt": read(st, "requirements.txt")
                          + "\nnumpy\n"}))
    check("reject: release missing a core file",
          rejected("v1.5.0", "missing",
                   drop=[os.path.join("services", "engine", "main.py")]))

    # ---- health check fails after install -> automatic rollback
    engine_py = "services/engine/supervisor.py"
    make_release("v1.6.0", edits={engine_py: read(st, engine_py)
                                  + "\n# v1.6.0\n"})
    LATEST["tag"] = "v1.6.0"
    restarts.clear()
    health["results"] = [(False, "did not come back healthy: engine"),
                         (True, "ok")]
    rc = updater.apply()
    s = updater.read_state()
    check("rollback: reported, old version still in place",
          rc == 1 and s["state"] == "rolled_back"
          and read(st, "VERSION").strip() == "1.1.0"
          and not read(st, engine_py).endswith("# v1.6.0\n"))
    check("rollback: same services restarted onto new code, then back",
          len(restarts) == 2 and restarts[0] == restarts[1]
          and "engine" in restarts[0])
    health["results"] = [(False, "engine"), (False, "engine")]
    restarts.clear()
    rc = updater.apply()
    check("rollback that ALSO fails is flagged loudly (call support)",
          rc == 1 and updater.read_state()["state"] == "failed"
          and "call support" in updater.read_state()["message"])
    # restore a sane station for the remaining checks
    shutil.rmtree(st, ignore_errors=True)
    st = make_station("1.1.0")
    use_station(st)

    # ---- guards
    b1, b2 = updater.backup("9.9.9"), updater.backup("9.9.9")
    check("guard: two backups in the same second don't collide",
          b1 != b2 and os.path.isdir(b1) and os.path.isdir(b2))
    lock = os.path.join(st, "data", "update.lock")
    open(lock, "w").close()
    check("guard: a second concurrent update is refused",
          updater.apply() == 1 and read(st, "VERSION").strip() == "1.1.0")
    os.remove(lock)
    os.makedirs(os.path.join(st, ".git"))
    check("guard: refuses on a developer git checkout",
          updater.apply() == 1 and "git" in updater.read_state()["message"])
    shutil.rmtree(os.path.join(st, ".git"))
    LATEST["tag"] = "v1.1.0"
    make_release("v1.0.5")
    restarts.clear()
    rc = updater.apply(tag="v1.0.5", force=True)
    check("--tag + --force installs an older release (manual rollback)",
          rc == 0 and read(st, "VERSION").strip() == "1.0.5")

    shutil.rmtree(st, ignore_errors=True)
    srv.shutdown()
    print(f"UPDATER OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
