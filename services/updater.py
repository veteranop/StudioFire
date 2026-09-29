"""StudioFire self-updater — GitHub Releases are the single source of truth.

A patch ships as a GitHub release (tag vX.Y.Z, cut with scripts/release.py).
Every deployed box can then update itself from it:

    python -m services.updater check            # is there a newer release?
    python -m services.updater apply            # install the latest release
    python -m services.updater apply --tag v1.2.0

The web GUI (Settings -> Software updates) runs the same `apply` in a
detached helper process.

What an update touches — ONLY the code the installer ships (MANAGED below):
services/, web/, scripts/*.py|*.bat, the root .bat files, VERSION,
CHANGELOG.md, requirements.txt, config/config.example.json. It never touches
config/config.json, data/ (DB), logs/, precache/, assets/, bin/ (mpv, nssm)
or runtime/ (bundled Python).

Safety, in order:
  1. Refuses on a developer git checkout (use git there) and when another
     update is already running.
  2. Downloads the release's source zip and VERIFIES it before touching
     anything: its VERSION must match the tag, the key files must exist,
     every .py must compile, and requirements.txt must be unchanged (new
     Python packages need the full installer — see DEPLOY.md).
  3. Backs up the current code and the database (data/updates/backup-*).
  4. Swaps the files in, then restarts ONLY what changed: the web GUI and
     library indexer when their code changed, the audio engine ONLY when
     engine code changed (the only step with a few seconds of off-air).
  5. Confirms each restarted service answers its health check and reports
     the new version; if not, restores the backup, restarts again, and
     reports "rolled back". The station is never left on half an update.

Progress/results go to data/update_state.json (the GUI polls it) and
logs/update.log. STDLIB ONLY: the updater must work even if a dependency is
broken — that may be exactly what the update fixes.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "veteranop/StudioFire"
DEFAULT_API = f"https://api.github.com/repos/{REPO}"
# where releases come from: env STUDIOFIRE_UPDATE_API, else config.json's
# top-level "update_api" (a private mirror / testing), else GitHub. The GUI's
# update helper is created via WMI and does NOT inherit env vars, hence the
# config option. None = resolve at call time (see api_base()).
API_BASE: str | None = None
USER_AGENT = "StudioFire-Updater"

# the installed code, exactly what installer/StudioFire.iss ships (keep the
# two in sync). Dirs are mirrored: files the new release no longer has are
# removed, so a deleted module can't linger and shadow anything.
MANAGED_DIRS = ["services", "web"]
MANAGED_SCRIPT_EXTS = (".py", ".bat")          # scripts/ (top level only)
MANAGED_FILES = ["start-all.bat", "stop-all.bat", "restart-all.bat",
                 "healthcheck.bat", "update.bat", "requirements.txt", "VERSION",
                 "CHANGELOG.md", os.path.join("config", "config.example.json")]
REQUIRED = [os.path.join("services", "engine", "main.py"),
            os.path.join("services", "core", "main.py"),
            os.path.join("services", "worker", "main.py"),
            os.path.join("web", "templates", "base.html"), "VERSION"]

SERVICES = ["engine", "core", "worker"]
NSSM_NAMES = {"engine": "StudioFireEngine", "core": "StudioFireWeb",
              "worker": "StudioFireWorker"}
HEALTH_TIMEOUT = 120.0     # seconds for a restarted service to come back
KEEP_BACKUPS = 3
LOCK_STALE_SEC = 45 * 60


class UpdateError(Exception):
    """A step failed; message is operator-readable."""


# ----------------------------------------------------------------- basics

def data_dir() -> str:
    return os.path.join(ROOT, "data")


def state_path() -> str:
    return os.path.join(data_dir(), "update_state.json")


def read_version(root: str | None = None) -> str:
    try:
        with open(os.path.join(root or ROOT, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


def parse_version(v: str) -> tuple:
    """'v1.2.3' / '1.2.3' -> (1, 2, 3). Junk parts count as 0."""
    out = []
    for part in (v or "").strip().lstrip("vV").split("-")[0].split(".")[:3]:
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def is_dev_checkout(root: str | None = None) -> bool:
    return os.path.isdir(os.path.join(root or ROOT, ".git"))


def _log(msg: str) -> None:
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open(os.path.join(ROOT, "logs", "update.log"), "a",
                  encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:     # a cp1252 console can't print '→'/'…' — logging must never
        print(line, flush=True)             # be what breaks an update
    except (UnicodeEncodeError, OSError, ValueError):
        try:
            print(line.encode("ascii", "replace").decode(), flush=True)
        except (OSError, ValueError):
            pass


def read_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_state(**kv) -> dict:
    st = read_state()
    st.update(kv)
    st["updated_at"] = time.time()
    os.makedirs(data_dir(), exist_ok=True)
    tmp = state_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, state_path())
    return st


def _step(state: str, message: str) -> None:
    _log(f"[{state}] {message}")
    _write_state(state=state, message=message)


def _station_config() -> dict:
    try:
        with open(os.path.join(ROOT, "config", "config.json"), "rb") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def api_base() -> str:
    return (API_BASE or os.environ.get("STUDIOFIRE_UPDATE_API")
            or _station_config().get("update_api") or DEFAULT_API).rstrip("/")


def load_ports() -> tuple[int, int]:
    """(web port, engine port) from config/config.json, with the defaults."""
    cfg = _station_config()
    return (int(cfg.get("core", {}).get("bind_port", 8080)),
            int(cfg.get("engine", {}).get("ipc_port", 7701)))


# ------------------------------------------------------------------ GitHub

def _get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_release(tag: str | None = None) -> dict:
    """The latest (or a specific) published release:
    {version, tag, name, notes, zip_url, html_url, published_at}."""
    url = f"{api_base()}/releases/" + (f"tags/{tag}" if tag else "latest")
    try:
        rel = json.loads(_get(url))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("no published release found on GitHub"
                              + (f" for {tag}" if tag else ""))
        raise UpdateError(f"GitHub said {exc.code}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise UpdateError(f"can't reach GitHub ({exc})")
    return {"version": ".".join(map(str, parse_version(rel["tag_name"]))),
            "tag": rel["tag_name"], "name": rel.get("name") or rel["tag_name"],
            "notes": rel.get("body") or "",
            "zip_url": rel["zipball_url"], "html_url": rel.get("html_url"),
            "published_at": rel.get("published_at")}


def check() -> dict:
    """Compare this box's VERSION with the latest release. Never raises."""
    current = read_version()
    out = {"current": current, "checked_at": time.time(),
           "dev_checkout": is_dev_checkout()}
    try:
        rel = fetch_release()
    except UpdateError as exc:
        out.update(error=str(exc), latest=None, update_available=False)
        return out
    out.update(error=None, latest=rel,
               update_available=parse_version(rel["version"])
               > parse_version(current))
    return out


# ------------------------------------------------------------ file sets

def managed_files(root: str) -> list[str]:
    """Relative paths of every managed file present under `root`."""
    rels = []
    for d in MANAGED_DIRS:
        base = os.path.join(root, d)
        for dirpath, dirnames, files in os.walk(base):
            dirnames[:] = [x for x in dirnames if x != "__pycache__"]
            for n in files:
                if n.endswith((".pyc", ".pyo")):
                    continue
                rels.append(os.path.relpath(os.path.join(dirpath, n), root))
    sdir = os.path.join(root, "scripts")
    if os.path.isdir(sdir):
        for n in os.listdir(sdir):
            if n.lower().endswith(MANAGED_SCRIPT_EXTS) and \
                    os.path.isfile(os.path.join(sdir, n)):
                rels.append(os.path.join("scripts", n))
    for f in MANAGED_FILES:
        if os.path.isfile(os.path.join(root, f)):
            rels.append(f)
    return sorted(set(rels))


def _digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def changed_files(new_root: str, old_root: str | None = None) -> list[str]:
    """Managed files that differ (added, changed or removed)."""
    old_root = old_root or ROOT
    new, old = set(managed_files(new_root)), set(managed_files(old_root))
    out = sorted((new ^ old))
    for rel in new & old:
        if _digest(os.path.join(new_root, rel)) != \
                _digest(os.path.join(old_root, rel)):
            out.append(rel)
    return sorted(set(out))


def services_to_restart(changed: list[str]) -> list[str]:
    """The audio engine only restarts for engine code (it's the one restart
    listeners hear). Web/indexer share services/core, so anything else
    restarts both of them."""
    norm = [c.replace("\\", "/") for c in changed]
    engine = any(c.startswith("services/engine/") or c == "services/__init__.py"
                 for c in norm)
    other = any(not c.startswith("services/engine/") for c in norm)
    return ([s for s, want in (("engine", engine), ("core", other),
                               ("worker", other)) if want])


def _norm_reqs(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.split("#")[0].strip().lower() for ln in f]
    except OSError:
        return []
    return sorted(ln for ln in lines if ln)


# -------------------------------------------------------- download/verify

def download_and_verify(rel: dict, work: str) -> str:
    """Download + unpack the release zip into `work`; return the unpacked
    code root after verifying it. Raises UpdateError."""
    zpath = os.path.join(work, rel["tag"] + ".zip")
    try:
        with open(zpath, "wb") as f:
            f.write(_get(rel["zip_url"], timeout=180))
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f"download failed ({exc})")
    try:
        with zipfile.ZipFile(zpath) as z:
            bad = z.testzip()
            if bad:
                raise UpdateError(f"download is corrupt ({bad})")
            for name in z.namelist():   # no absolute / parent-escaping paths
                if name.startswith(("/", "\\")) or ".." in name.split("/"):
                    raise UpdateError(f"unsafe path in download: {name}")
            z.extractall(os.path.join(work, "unpacked"))
    except zipfile.BadZipFile:
        raise UpdateError("download is not a valid zip")
    top = os.listdir(os.path.join(work, "unpacked"))
    # GitHub zipballs wrap everything in one '<owner>-<repo>-<sha>/' folder
    new_root = os.path.join(work, "unpacked", top[0]) if len(top) == 1 \
        else os.path.join(work, "unpacked")
    for req in REQUIRED:
        if not os.path.isfile(os.path.join(new_root, req)):
            raise UpdateError(f"release is missing {req}")
    got = read_version(new_root)
    if parse_version(got) != parse_version(rel["version"]):
        raise UpdateError(f"release {rel['tag']} contains VERSION {got} — "
                          "it was published wrong; not installing it")
    for rel_path in managed_files(new_root):
        if rel_path.endswith(".py"):
            full = os.path.join(new_root, rel_path)
            try:
                with open(full, "rb") as f:
                    compile(f.read(), full, "exec")
            except (SyntaxError, ValueError) as exc:
                raise UpdateError(f"release has broken code: {rel_path} "
                                  f"({exc})")
    if _norm_reqs(os.path.join(new_root, "requirements.txt")) != \
            _norm_reqs(os.path.join(ROOT, "requirements.txt")):
        raise UpdateError("this release needs new Python packages — install "
                          "it with the full StudioFire installer instead")
    return new_root


# ------------------------------------------------------- install/backup

def _copy_set(src_root: str, dst_root: str, rels: list[str]) -> None:
    for rel in rels:
        dst = os.path.join(dst_root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(src_root, rel), dst)


def backup(target_version: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(data_dir(), "updates",
                        f"backup-{read_version()}-to-{target_version}-{stamp}")
    bdir, n = base, 1
    while True:        # a retry within the same second must not collide
        try:
            os.makedirs(bdir)
            break
        except FileExistsError:
            n += 1
            bdir = f"{base}-{n}"
    _copy_set(ROOT, os.path.join(bdir, "code"), managed_files(ROOT))
    db = os.path.join(data_dir(), "studiofire.db")
    if os.path.isfile(db):       # online-safe copy while P2 keeps running
        src = sqlite3.connect(db)
        dst = sqlite3.connect(os.path.join(bdir, "studiofire.db"))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    return bdir


def install_tree(src_root: str) -> None:
    """Make the managed code under ROOT exactly match `src_root`'s."""
    new = managed_files(src_root)
    for rel in set(managed_files(ROOT)) - set(new):
        try:
            os.remove(os.path.join(ROOT, rel))
        except OSError:
            pass
    _copy_set(src_root, ROOT, new)


def check_writable() -> None:
    for d in MANAGED_DIRS + ["scripts", "config", "."]:
        path = os.path.join(ROOT, d)
        try:
            fd, tmp = tempfile.mkstemp(dir=path, prefix=".sf-write-test-")
            os.close(fd)
            os.remove(tmp)
        except OSError as exc:
            raise UpdateError(f"can't write to {path} ({exc.strerror}) — run "
                              "the update as an account that can modify the "
                              "StudioFire folder")


def prune_backups() -> None:
    base = os.path.join(data_dir(), "updates")
    try:
        olds = sorted((d for d in os.listdir(base) if d.startswith("backup-")),
                      key=lambda d: os.path.getmtime(os.path.join(base, d)))
    except OSError:
        return
    for d in olds[:-KEEP_BACKUPS]:
        shutil.rmtree(os.path.join(base, d), ignore_errors=True)


# ------------------------------------------------------------- restarts

def nssm_managed() -> bool:
    try:
        return subprocess.run(["sc", "query", NSSM_NAMES["engine"]],
                              capture_output=True).returncode == 0
    except OSError:
        return False


def service_pids() -> dict[str, list[str]]:
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "ForEach-Object { if ($_.CommandLine -match "
          "'services\\.(engine|core|worker)\\.main') { "
          "\"$($_.ProcessId) $($Matches[1])\" } }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True)
    pids: dict[str, list[str]] = {}
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit():
            pids.setdefault(parts[1], []).append(parts[0])
    return pids


def restart(names: list[str]) -> None:
    """Restart just these services. Under NSSM, killing the process is
    enough: NSSM revives it (with the new code) ~2s later. Otherwise
    relaunch detached, like scripts/restart_all.py. The engine goes last
    down / first up to keep any off-air gap minimal."""
    nssm = nssm_managed()
    pids = service_pids()
    order = [s for s in ("worker", "core", "engine") if s in names]
    for name in order:
        for pid in pids.get(name, []):
            args = ["taskkill", "/F", "/PID", pid]
            if name == "engine":
                args.insert(2, "/T")          # take its mpv child too
            subprocess.run(args, capture_output=True)
            _log(f"stopped {name} (pid {pid})")
    if nssm:
        _log("NSSM services will restart themselves")
        return
    time.sleep(1.5)
    # a detached relaunch doesn't inherit the logged-in session's mapped drives,
    # so re-map the NAS (config\drive-map.bat, if present) before starting — else
    # legacy Z:\ data would fail every isdir/isfile after the update restart.
    # Best-effort: never let a mapping error block the update.
    bat = os.path.join(ROOT, "config", "drive-map.bat")
    if os.path.exists(bat):
        try:
            subprocess.run(["cmd", "/c", bat], cwd=ROOT,
                           capture_output=True, timeout=30)
            _log("ran config\\drive-map.bat")
        except Exception as exc:  # noqa: BLE001 — mapping is best-effort
            _log(f"config\\drive-map.bat failed ({exc}); continuing")
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    cfg = os.path.join("config", "config.json")
    for name in reversed(order):              # engine first back up
        try:
            logf = open(os.path.join(ROOT, "logs", name + "_console.log"), "ab")
        except OSError:
            logf = subprocess.DEVNULL
        p = subprocess.Popen([sys.executable, "-m", f"services.{name}.main",
                              cfg], cwd=ROOT, stdout=logf, stderr=logf,
                             stdin=subprocess.DEVNULL, creationflags=flags,
                             close_fds=True)
        _log(f"launched {name} (pid {p.pid})")
        time.sleep(1.0)


def _http_json(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return json.loads(r.read()) if r.status == 200 else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_healthy(names: list[str], version: str,
                 timeout: float = HEALTH_TIMEOUT) -> tuple[bool, str]:
    """Every restarted service answers again (and the web GUI reports
    `version`). The indexer has no health endpoint — it's checked by being
    a running process."""
    web_port, engine_port = load_ports()
    start = time.monotonic()
    deadline = start + timeout
    pending = set(names)
    gone: dict[str, int] = {}   # consecutive polls with no process at all
    while pending and time.monotonic() < deadline:
        # fail fast when a restarted service has simply died (e.g. a release
        # that crashes on startup) instead of waiting out the full timeout —
        # every second here can be a second with no web GUI. (Under NSSM a
        # crash-looping service keeps reappearing, so that case still waits.)
        if time.monotonic() - start > 10:
            alive = service_pids()
            for n in pending:
                gone[n] = 0 if alive.get(n) else gone.get(n, 0) + 1
            dead = [n for n in pending if gone.get(n, 0) >= 3]
            if dead:
                return False, "crashed on startup: " + ", ".join(sorted(dead))
        if "engine" in pending:
            st = _http_json(f"http://127.0.0.1:{engine_port}/status")
            if st is not None and st.get("mpv_alive", True):
                pending.discard("engine")
        if "core" in pending:
            h = _http_json(f"http://127.0.0.1:{web_port}/health")
            # builds older than the updater don't report a version (only
            # matters when rolling back to one) — then 'ok' is enough
            if h and h.get("ok") and h.get("version", version) == version:
                pending.discard("core")
        if "worker" in pending and service_pids().get("worker"):
            pending.discard("worker")
        if pending:
            time.sleep(2.0)
    if pending:
        return False, "did not come back healthy: " + ", ".join(sorted(pending))
    return True, "ok"


# ----------------------------------------------------------------- apply

def _take_lock() -> str:
    lock = os.path.join(data_dir(), "update.lock")
    os.makedirs(data_dir(), exist_ok=True)
    try:
        if time.time() - os.path.getmtime(lock) > LOCK_STALE_SEC:
            os.remove(lock)                   # a crashed earlier run
    except OSError:
        pass
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        raise UpdateError("another update is already running")
    return lock


def apply(tag: str | None = None, force: bool = False,
          allow_git: bool = False) -> int:
    """Install the latest (or `tag`) release. Returns 0 on success/no-op,
    1 on failure (anything already changed is rolled back)."""
    if is_dev_checkout() and not allow_git:
        _write_state(state="failed", message="this is a developer (git) "
                     "checkout — update it with git, not the updater")
        _log("refusing: developer git checkout")
        return 1
    try:
        lock = _take_lock()
    except UpdateError as exc:
        _log(str(exc))
        return 1
    current = read_version()
    _write_state(state="starting", message="checking GitHub…", log=[],
                 from_version=current, to_version=None,
                 started_at=time.time(), finished_at=None)
    work = tempfile.mkdtemp(prefix="sf-update-",
                            dir=os.path.join(data_dir()))
    bdir = None
    installed = False
    restarted: list[str] = []
    try:
        rel = fetch_release(tag)
        _write_state(to_version=rel["version"])
        if not force and parse_version(rel["version"]) <= \
                parse_version(current):
            _step("done", f"already up to date ({current})")
            return 0
        _step("downloading", f"downloading {rel['tag']}…")
        new_root = download_and_verify(rel, work)
        changed = changed_files(new_root)
        which = services_to_restart(changed)
        _log(f"{len(changed)} file(s) change; restart: {which or 'nothing'}")
        check_writable()
        _step("backing_up", "backing up the current version + database…")
        bdir = backup(rel["version"])
        _write_state(backup_dir=bdir)
        _step("installing", f"installing {rel['version']}…")
        installed = True          # set BEFORE copying: a half-done copy
        install_tree(new_root)    # (locked file, disk full) must roll back
        if which:
            _step("restarting", "restarting: " + ", ".join(which) +
                  (" (audio drops for a few seconds)" if "engine" in which
                   else " (audio keeps playing)"))
            restarted = which     # before: a restart that dies midway
            restart(which)        # must still be undone
            ok, why = wait_healthy(which, rel["version"])
            if not ok:
                raise UpdateError(why)
        _step("done", f"updated {current} → {rel['version']}")
        _write_state(finished_at=time.time())
        prune_backups()
        return 0
    except Exception as exc:  # noqa: BLE001 — ANY failure: roll back
        if not isinstance(exc, UpdateError):
            _log(f"unexpected error: {exc!r}")
            exc = UpdateError(f"unexpected error ({exc})")
        _log(f"update failed: {exc}")
        if installed and bdir:
            _step("rolling_back", f"{exc} — restoring {current}…")
            try:
                install_tree(os.path.join(bdir, "code"))
                if restarted:
                    restart(restarted)
                    ok, why = wait_healthy(restarted, current)
                    if not ok:
                        _log(f"ROLLBACK HEALTH CHECK FAILED: {why}")
                        _write_state(state="failed", finished_at=time.time(),
                                     message=f"update failed ({exc}) and the "
                                     f"restored {current} is not healthy "
                                     f"({why}) — call support")
                        return 1
                _write_state(state="rolled_back", finished_at=time.time(),
                             message=f"update failed ({exc}); still running "
                             f"{current}")
            except Exception as rb_exc:  # noqa: BLE001
                _log(f"ROLLBACK FAILED: {rb_exc}")
                _write_state(state="failed", finished_at=time.time(),
                             message=f"update failed ({exc}) and rollback "
                             f"failed ({rb_exc}) — backup: {bdir}")
            return 1
        _write_state(state="failed", message=str(exc),
                     finished_at=time.time())
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
        try:
            os.remove(lock)
        except OSError:
            pass


def spawn_detached_apply(tag: str | None = None) -> None:
    """Start `apply` in a process that is NOT a child of the caller (the web
    GUI is one of the services the update restarts). Created through WMI so
    it's outside the caller's process tree and any NSSM job; falls back to a
    plain detached process."""
    args = [sys.executable, "-m", "services.updater", "apply"]
    if tag:
        args += ["--tag", tag]
    cmdline = subprocess.list2cmdline(args)
    ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
          "-Arguments @{CommandLine=" + _ps_quote(cmdline) +
          "; CurrentDirectory=" + _ps_quote(ROOT) + "}; "
          "exit $r.ReturnValue")
    try:
        rc = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                            capture_output=True, timeout=30).returncode
        if rc == 0:
            return
        _log(f"WMI launch returned {rc}; falling back to detached process")
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"WMI launch failed ({exc}); falling back to detached process")
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
             | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0))
    subprocess.Popen(args, cwd=ROOT, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m services.updater",
                                 description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="is a newer release on GitHub?")
    a = sub.add_parser("apply", help="install the latest (or --tag) release")
    a.add_argument("--tag", help="a specific release tag, e.g. v1.2.0")
    a.add_argument("--force", action="store_true",
                   help="install even if it isn't newer (e.g. a rollback)")
    a.add_argument("--allow-git", action="store_true",
                   help="allow running on a git checkout (testing only)")
    args = ap.parse_args(argv)
    if args.cmd == "check":
        r = check()
        if r.get("error"):
            print(f"StudioFire {r['current']} — can't check: {r['error']}")
            return 1
        latest = r["latest"]["version"]
        print(f"StudioFire {r['current']} — latest release {latest}: "
              + ("UPDATE AVAILABLE" if r["update_available"]
                 else "up to date"))
        return 0
    return apply(args.tag, args.force, args.allow_git)


if __name__ == "__main__":
    sys.exit(main())
