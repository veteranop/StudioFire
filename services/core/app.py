"""P2 FastAPI application factory.

Pages are server-rendered Jinja2 (+ HTMX later, task: web GUI). This module
owns the app skeleton: sessions, first-run setup, login/logout, and the
auth dependencies every later router builds on.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import logging
import os
import sys
import time

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (auth, changelog, db, nowplaying_file, reports,
                  setup as setup_mod, spots)
from . import schedule as sched
from .. import pathmap

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(ROOT, "web")
log = logging.getLogger("core.app")


def index_browse(conn, path: str, want: set, root: str) -> dict | None:
    """List the immediate sub-folders/files of `path` FROM THE INDEX (local DB)
    instead of a live os.listdir, when `path` is inside the music root. Browsing
    /G live is thousands of SMB round-trips (~3 min over a VPN); the index has
    every path already, so this is instant. Read-only — safe alongside the
    indexer's writes (WAL). Returns None when `path` is outside the music root
    (caller then lists it live). Only shows what's been indexed so far.
    """
    if not root:
        return None
    nroot = os.path.normcase(os.path.normpath(root))
    ntarget = os.path.normcase(os.path.normpath(path))
    if ntarget != nroot and not ntarget.startswith(nroot + os.sep):
        return None  # not under the music root — list it live
    rel = ntarget[len(nroot):].strip("\\/")
    # rebuild the stored-path prefix: the root exactly as indexed + backslash rel
    prefix = root if not rel else root.rstrip("\\/") + os.sep + rel.replace("/", os.sep)
    rows = conn.execute(
        "SELECT path FROM tracks WHERE missing = 0 AND path LIKE ?",
        (prefix + os.sep + "%",)).fetchall()
    cut = len(prefix) + 1  # strip "prefix\" (case differs but length matches)
    dirs, files = set(), set()
    for r in rows:
        parts = r["path"][cut:].replace("/", os.sep).split(os.sep)
        if len(parts) == 1:                       # a file directly in this dir
            if want and os.path.splitext(parts[0])[1].lower() in want:
                files.add(parts[0])
        elif parts[0]:                            # a sub-folder
            dirs.add(parts[0])
    absn = os.path.abspath(path)
    return {"path": absn, "parent": os.path.dirname(absn.rstrip("\\/")),
            "dirs": sorted(dirs, key=str.lower),
            "files": sorted(files, key=str.lower), "from_index": True}


def create_app(cfg: dict) -> FastAPI:
    db.migrate(cfg["db_path"])
    sessions = auth.Sessions(auth.load_secret(cfg["secret_path"]))
    templates = Jinja2Templates(directory=os.path.join(WEB, "templates"))
    # env globals so EVERY render (routers included) sees them without
    # threading extra ctx through each handler
    #
    # Telemetry is OPT-IN. ``ga_id`` is empty unless an operator turned it on,
    # and base.html keys the entire GA4 tag off it — so an untouched install
    # ships no tracking script at all. ``ga_install_id`` is a random value
    # minted at opt-in; the station name is deliberately NOT exposed to the
    # templates, so it cannot ride along on anything that leaves the box.
    def _telemetry_globals(c: dict) -> None:
        on = bool(c.get("telemetry_enabled")) and bool(c.get("telemetry_install_id"))
        templates.env.globals["ga_id"] = (c.get("ga_measurement_id") or "") if on else ""
        templates.env.globals["ga_install_id"] = (
            (c.get("telemetry_install_id") or "") if on else "")
    _telemetry_globals(cfg)
    # cache-buster for /static/style.css: after an update the browser fetches
    # the new stylesheet instead of showing the old layout until a hard refresh
    # (read on every render — one stat — so a stylesheet updated while the
    # service runs is picked up too, not only after a restart)
    class _AssetVersion:
        def __str__(self):
            try:
                return str(int(os.path.getmtime(
                    os.path.join(WEB, "static", "style.css"))))
            except OSError:
                return "0"
    templates.env.globals["asset_v"] = _AssetVersion()
    # lets the changelog template render a **bold lead-in** as <strong> while
    # every text segment is still auto-escaped by Jinja (no injection, and Z:\
    # backslashes in the notes survive)
    templates.env.filters["sf_bold"] = changelog.bold_segments
    app = FastAPI(title="StudioFire", docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.sessions = sessions
    app.state.templates = templates
    app.state.refresh_telemetry = _telemetry_globals
    app.mount("/static", StaticFiles(directory=os.path.join(WEB, "static")),
              name="static")

    # ------------------------------------------------------------ helpers

    def get_conn():
        conn = db.connect(cfg["db_path"])
        try:
            yield conn
        finally:
            conn.close()

    def session_of(request: Request) -> dict | None:
        return sessions.read(request.cookies.get(auth.SESSION_COOKIE))

    def page_user(request: Request):
        """Page guard: bounce anonymous users to /login (or /setup)."""
        sess = session_of(request)
        if sess is None:
            raise HTTPException(status_code=303,
                                headers={"Location": "/login"})
        return sess

    def api_user(request: Request):
        sess = session_of(request)
        if sess is None:
            raise HTTPException(status_code=401, detail="not signed in")
        return sess

    def api_admin(sess: dict = Depends(api_user)):
        if sess["role"] != "admin":
            raise HTTPException(status_code=403, detail="admin only")
        return sess

    app.state.page_user = page_user      # routers added later reuse these
    app.state.api_user = api_user
    app.state.api_admin = api_admin
    app.state.get_conn = get_conn

    # ---- clock display format (operator chooses military or AM/PM in Settings)
    def time_format(conn) -> str:
        """The station's saved clock format: '12' (AM/PM) or '24' (military).
        Defaults to 12-hour when the operator has never chosen."""
        v = db.get_setting(conn, "time_format")
        return v if v in ("12", "24") else "12"

    def apply_time_format(conn) -> bool:
        """Push the saved format into the schedule module (so server-rendered
        'when'/'next' labels match) and return it as a bool (True = 12-hour)."""
        h12 = time_format(conn) == "12"
        sched.set_hour12(h12)
        return h12

    _startup_conn = db.connect(cfg["db_path"])
    try:                       # apply the saved format before the first render
        apply_time_format(_startup_conn)
    finally:
        _startup_conn.close()

    def render(request, name, **ctx):
        ctx.setdefault("station", cfg["station_name"])
        ctx.setdefault("hour12", sched.hour12())
        return templates.TemplateResponse(request, name, ctx)

    app.state.render = render           # routers added later reuse this

    # ------------------------------------------------------------- routes

    from .. import updater
    # read ONCE at startup: this is the version of the code actually running
    # (the updater swaps VERSION on disk before this process restarts, and
    # uses this to confirm the NEW code is what came back up)
    running_version = updater.read_version()
    app.state.running_version = running_version

    @app.get("/health")
    def health():
        return {"ok": True, "service": "core", "version": running_version}

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, conn=Depends(get_conn)):
        """The first-run wizard. One step per screen, revisitable from Settings.

        Before an admin exists this page is open (a fresh install has no users
        and nothing to protect). After that it needs a signed-in admin, and once
        the wizard is finished it gets out of the way.
        """
        from .config import load_config
        st = setup_mod.state(conn, load_config())
        if st["complete"]:
            return RedirectResponse("/", status_code=303)
        if auth.any_users(conn) and session_of(request) is None:
            return RedirectResponse("/login", status_code=303)
        if not auth.any_users(conn) and st["step"] not in ("station", "admin"):
            setup_mod.set_step(conn, "station")
            st["step"] = "station"
            st["index"] = 0
        return render(request, "setup.html", setup=st, station=st["answers"].get(
            "station_name") or "StudioFire")

    @app.post("/setup")
    def setup_submit(request: Request, conn=Depends(get_conn),
                     username: str = Form(...), password: str = Form(...),
                     password2: str = Form(...)):
        if auth.any_users(conn):  # setup runs exactly once
            return RedirectResponse("/login", status_code=303)
        if not username.strip() or len(password) < 8 or password != password2:
            from .config import load_config as _lc
            return render(request, "setup.html",
                          setup=setup_mod.state(conn, _lc()),
                          station=(_lc().get("station_name")
                                   or "StudioFire"),
                          error="Passwords must match and be at least "
                                "8 characters.")
        uid = auth.create_user(conn, username.strip(), password, "admin")
        try:
            setup_mod.set_step(conn, "library")
        except Exception:       # noqa: BLE001 — never fail account creation
            pass
        resp = RedirectResponse("/setup", status_code=303)
        resp.set_cookie(auth.SESSION_COOKIE, sessions.issue(uid, "admin"),
                        httponly=True, samesite="lax",
                        max_age=auth.SESSION_MAX_AGE)
        log.info("first-run setup: admin %r created", username.strip())
        return resp


    # ---- first-run wizard ---------------------------------------------------
    @app.get("/api/setup/state")
    def api_setup_state(request: Request, conn=Depends(get_conn)):
        from .config import load_config
        if auth.any_users(conn) and session_of(request) is None:
            raise HTTPException(401, "not signed in")
        return setup_mod.state(conn, load_config())

    @app.post("/api/setup/step")
    def api_setup_step(request: Request, body: dict, conn=Depends(get_conn)):
        """Save one wizard step, then move to the next one."""
        from .config import load_config, update_config, set_telemetry
        from . import playlists as pl
        step = (body.get("step") or "").strip()
        values = body.get("values") or {}
        fresh = not auth.any_users(conn)
        if fresh and step != "station":
            raise HTTPException(401, "create the administrator account first")
        if not fresh and session_of(request) is None:
            raise HTTPException(401, "not signed in")

        if step == "station":
            name = (values.get("station_name") or "").strip() or "StudioFire"
            update_config({"station_name": name})
            fmt = "12" if str(values.get("time_format") or "12") == "12" else "24"
            db.set_setting(conn, "time_format", fmt)
            sched.set_hour12(fmt == "12")
        elif step == "library":
            root = (values.get("music_root") or "").strip().strip('"')
            problem = setup_mod.check_folder(root)
            if problem:
                raise HTTPException(400, problem)
            update_config({"paths": {"nas_music_root": root}})
            found = setup_mod.count_audio_files(root)
            setup_mod.set_step(conn, "playlists")
            return {"ok": True, "next": "playlists", "found_songs": found}
        elif step == "playlists":
            folder = (values.get("playlists_dir") or "").strip().strip('"')
            if folder and setup_mod.check_folder(folder) is None:
                db.set_setting(conn, "playlist_lst_dir", folder)
            elif folder:
                raise HTTPException(400, setup_mod.check_folder(folder))
        elif step == "spots":
            for key in ("dir_station_ids", "dir_psas", "dir_jingles",
                        "dir_ads", "dir_shows"):
                path = (values.get(key) or "").strip().strip('"')
                if path:
                    problem = setup_mod.check_folder(path)
                    if problem:
                        raise HTTPException(400, "%s: %s" % (key, problem))
                    db.set_setting(conn, key, path)
        elif step == "audio":
            guid = (values.get("audio_device_guid") or "").strip()
            update_config({"engine": {"audio_device_guid": guid}})
        elif step == "emergency":
            db.set_setting(conn, "emergency_fill_from_library",
                           "1" if values.get("fill_from_library") else "0")
        elif step == "streaming":
            if values.get("np_file") is not None:
                db.set_setting(conn, nowplaying_file.K_PATH,
                               (values.get("np_file") or "").strip())
        elif step == "done":
            # Telemetry is asked ONCE, here, and the checkbox is unchecked by
            # default. An absent answer (Skip, or a stale wizard page in
            # another tab) changes nothing, so nobody is ever opted in by
            # omission — only an explicit tick sends anything.
            if "telemetry" in values:
                t = set_telemetry(bool(values.get("telemetry")))
                _telemetry_globals({
                    "telemetry_enabled": t["enabled"],
                    "telemetry_install_id": t["install_id"],
                    "ga_measurement_id": t["measurement_id"],
                })
            setup_mod.complete(conn)
            return {"ok": True, "next": "done", "complete": True}
        else:
            raise HTTPException(400, "unknown step")

        ids = [s for s, _ in setup_mod.STEPS]
        try:
            nxt = ids[ids.index(step) + 1]
        except (ValueError, IndexError):
            nxt = "done"
        setup_mod.set_step(conn, nxt)
        return {"ok": True, "next": nxt}

    @app.get("/api/setup/settings-copy")
    def api_setup_settings_copy(_=Depends(api_user)):
        """A JSON copy of the station's setup, for the operator to keep.

        Secrets are never included: the stream password lives in an
        environment-sourced secret file, and the session key is not settings.
        """
        from .config import load_config
        cfg = load_config()
        safe = {
            "station_name": cfg.get("station_name"),
            "paths": {k: v for k, v in (cfg.get("paths") or {}).items()
                      if "secret" not in k and "password" not in k},
            "engine": {"audio_device_guid":
                       (cfg.get("engine") or {}).get("audio_device_guid", "")},
            "royalty_reporting": {k: v for k, v in
                                  (cfg.get("royalty_reporting") or {}).items()
                                  if k != "contact"},
        }
        return Response(content=json.dumps(safe, indent=2),
                        media_type="application/json",
                        headers={"Content-Disposition":
                                 "attachment; filename=studiofire-settings.json"})

    @app.get("/api/setup/audio-devices")
    def api_setup_audio(_=Depends(api_user)):
        return {"devices": setup_mod.audio_devices()}

    @app.post("/api/setup/test-tone")
    def api_setup_tone(body: dict, _=Depends(api_user)):
        ok, note = setup_mod.test_tone((body or {}).get("guid") or "")
        return {"ok": ok, "note": note}

    @app.post("/api/setup/library-count")
    def api_setup_library_count(body: dict, _=Depends(api_user)):
        root = (body or {}).get("path") or ""
        problem = setup_mod.check_folder(root)
        if problem:
            raise HTTPException(400, problem)
        return {"found": setup_mod.count_audio_files(root)}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, conn=Depends(get_conn)):
        if not auth.any_users(conn):
            return RedirectResponse("/setup", status_code=303)
        return render(request, "login.html")

    @app.post("/login")
    def login_submit(request: Request, conn=Depends(get_conn),
                     username: str = Form(...), password: str = Form(...)):
        user = auth.authenticate(conn, username.strip(), password)
        if user is None:
            return render(request, "login.html",
                          error="Wrong username or password.")
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(auth.SESSION_COOKIE,
                        sessions.issue(user["id"], user["role"]),
                        httponly=True, samesite="lax",
                        max_age=auth.SESSION_MAX_AGE)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.SESSION_COOKIE)
        return resp

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, sess: dict = Depends(page_user)):
        return render(request, "dashboard.html", role=sess["role"],
                      allow_restart=cfg.get("allow_gui_restart", True))

    @app.post("/api/system/restart")
    def api_system_restart(_=Depends(api_user)):
        """Cycle all StudioFire services via a console-independent Python helper
        launched detached (so it survives P2 being killed and relaunched).
        Guarded by allow_gui_restart — turn it off on the on-air PC."""
        if not cfg.get("allow_gui_restart", True):
            raise HTTPException(403, "GUI restart is disabled on this machine")
        import subprocess
        helper = os.path.join(ROOT, "scripts", "restart_all.py")
        if not os.path.exists(helper):
            raise HTTPException(400, "scripts/restart_all.py not found")
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        # Spawn with P2's OWN interpreter (Anaconda, with our deps — not the
        # Windows Store `python` stub on PATH). The helper relaunches every
        # service detached; a batch file's `start cmd /k` can't create windows
        # from this no-console process, so the old approach stopped everything
        # but never brought it back (box stuck on emergency filler, UI down).
        subprocess.Popen([sys.executable, helper], cwd=ROOT, close_fds=True,
                         stdin=subprocess.DEVNULL, creationflags=flags)
        log.warning("GUI-triggered full restart via %s", helper)
        return {"ok": True}

    @app.post("/api/system/quit")
    def api_system_quit(_=Depends(api_user)):
        """Quit all StudioFire services (stop without restarting) via a
        console-independent Python helper launched detached. Requires user login."""
        import subprocess
        helper = os.path.join(ROOT, "scripts", "quit_all.py")
        if not os.path.exists(helper):
            raise HTTPException(400, "scripts/quit_all.py not found")
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        subprocess.Popen([sys.executable, helper], cwd=ROOT, close_fds=True,
                         stdin=subprocess.DEVNULL, creationflags=flags)
        log.warning("GUI-triggered quit via %s", helper)
        return {"ok": True}

    @app.get("/playlists", response_class=HTMLResponse)
    def playlists_page(request: Request, sess: dict = Depends(page_user),
                       conn=Depends(get_conn)):
        from . import playlists as pl
        return render(request, "playlists.html", role=sess["role"],
                      playlists=pl.list_playlists(conn))

    @app.get("/playlists/{pid}", response_class=HTMLResponse)
    def playlist_edit_page(pid: int, request: Request,
                           sess: dict = Depends(page_user),
                           conn=Depends(get_conn)):
        from . import playlists as pl
        row = conn.execute("SELECT * FROM playlists WHERE id = ?",
                           (pid,)).fetchone()
        if row is None:
            raise HTTPException(404)
        return render(request, "playlist_edit.html", role=sess["role"],
                      playlist=dict(row), items=pl.get_items(conn, pid))

    # ---- settings: station folders (used by scheduling + spots + builder).
    # Shared with the spot picker so the two never drift apart.
    DIR_SETTINGS = spots.FOLDER_CATEGORIES

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, sess: dict = Depends(page_user),
                      conn=Depends(get_conn)):
        # Basic (operator) can do everything except manage users; the Users
        # section is only rendered/enabled for admins (role passed to template).
        # The change log is read from CHANGELOG.md on disk (tolerant: a missing
        # file just renders "not available", never a 500).
        try:
            uname = (auth.get_user(conn, sess["uid"]) or {}).get("username") or ""
        except Exception:      # noqa: BLE001 — never break the page over a label
            uname = ""
        return render(request, "settings.html", role=sess["role"],
                      user=uname,
                      changelog=changelog.load(),
                      running_version=running_version)

    @app.get("/changelog", response_class=HTMLResponse)
    def changelog_page(request: Request, sess: dict = Depends(page_user)):
        """The full release history, moved off Settings so that page can stay
        one group per screen. Read-only."""
        return render(request, "changelog.html", role=sess["role"],
                      changelog=changelog.load(),
                      running_version=running_version)

    @app.get("/api/changelog")
    def api_changelog(_=Depends(api_user)):
        """The parsed release history (newest first) plus the running version.
        Read-only; any signed-in operator can read it."""
        cl = changelog.load()
        return {"current": running_version, "ok": cl["ok"],
                "error": cl["error"], "releases": cl["releases"]}

    @app.get("/api/settings/dirs")
    def get_dirs(conn=Depends(get_conn), _=Depends(api_user)):
        from . import playlists as pl
        # 'exists' is checked against the resolved path so a Z:\ station folder
        # reports the truth (mapped -> NAS share root) instead of "does not
        # exist" when this session has no drive mapping
        return [{"key": k, "label": lbl, "hint": hint,
                 "path": db.get_setting(conn, k) or "",
                 "exists": os.path.isdir(pl.alias_path(db.get_setting(conn, k)
                                                       or ""))}
                for k, lbl, hint in DIR_SETTINGS]

    @app.post("/api/settings/dirs")
    def set_dir(body: dict, conn=Depends(get_conn), _=Depends(api_user)):
        from . import playlists as pl
        key, path = body.get("key"), (body.get("path") or "").strip()
        if key not in {k for k, _, _ in DIR_SETTINGS}:
            raise HTTPException(400, "unknown setting")
        # validate against the resolved path (a Z:\ folder that maps is valid);
        # store what the operator entered — resolve, don't rewrite
        if path and not os.path.isdir(pl.alias_path(path)):
            raise HTTPException(400, "that folder does not exist")
        db.set_setting(conn, key, path)
        return {"ok": True}


    @app.get("/api/settings/support_contact")
    def get_support_contact(conn=Depends(get_conn), _=Depends(api_user)):
        """Who a volunteer calls when something looks wrong. Blank means the
        Help page falls back to "ask whoever set StudioFire up"."""
        return {"contact": db.get_setting(conn, "support_contact") or ""}

    @app.post("/api/settings/support_contact")
    def set_support_contact(body: dict, conn=Depends(get_conn),
                            _=Depends(api_user)):
        db.set_setting(conn, "support_contact",
                       (body.get("contact") or "").strip()[:200])
        return {"ok": True}

    @app.get("/api/settings/time_format")
    def get_time_format(conn=Depends(get_conn), _=Depends(api_user)):
        """The station's clock display format: '12' (AM/PM) or '24' (military)."""
        return {"format": time_format(conn)}

    @app.post("/api/settings/time_format")
    def set_time_format(body: dict, conn=Depends(get_conn), _=Depends(api_user)):
        """Switch the clock display between 12-hour (AM/PM) and 24-hour
        (military). Applies to the header clock, the On Air log, reports, the
        schedule, and the server-rendered show labels."""
        fmt = str(body.get("format") or "").strip()
        if fmt not in ("12", "24"):
            raise HTTPException(400, "format must be '12' or '24'")
        db.set_setting(conn, "time_format", fmt)
        sched.set_hour12(fmt == "12")   # live — affects the next label render
        return {"ok": True, "format": fmt}

    # ---- anonymous feature-usage telemetry (opt-in, admin only)
    @app.get("/api/settings/telemetry")
    def get_telemetry(conn=Depends(get_conn), _=Depends(api_user)):
        """Off unless someone turned it on. The install ID is shown so an
        operator can see exactly what identifier their box would report."""
        from .config import load_config
        c = load_config()
        return {"enabled": bool(c.get("telemetry_enabled")),
                "install_id": c.get("telemetry_install_id") or ""}

    @app.post("/api/settings/telemetry")
    def set_telemetry_settings(body: dict, _=Depends(api_admin)):
        """Turn feature-usage counting on or off. Admin only: it is a change to
        what the station reports about itself, not a display preference."""
        from .config import set_telemetry
        t = set_telemetry(bool((body or {}).get("enabled")))
        _telemetry_globals({
            "telemetry_enabled": t["enabled"],
            "telemetry_install_id": t["install_id"],
            "ga_measurement_id": t["measurement_id"],
        })
        return {"ok": True, "enabled": t["enabled"],
                "install_id": t["install_id"]}

    # ---- user administration (admin only: the one thing Basic can't do)
    def _norm_role(r: str | None) -> str | None:
        r = (r or "").strip().lower()
        return "admin" if r == "admin" else \
            "operator" if r in ("basic", "operator") else None

    @app.get("/api/users")
    def api_users_list(conn=Depends(get_conn), _=Depends(api_admin)):
        return auth.list_users(conn)

    @app.post("/api/users", status_code=201)
    def api_users_create(body: dict, conn=Depends(get_conn),
                         _=Depends(api_admin)):
        import sqlite3
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        role = _norm_role(body.get("role"))
        if not username:
            raise HTTPException(400, "username required")
        if len(password) < 8:
            raise HTTPException(400, "password must be at least 8 characters")
        if role is None:
            raise HTTPException(400, "role must be Admin or Basic")
        try:
            uid = auth.create_user(conn, username, password, role)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "a user with that name already exists")
        return {"id": uid}

    @app.delete("/api/users/{uid}")
    def api_users_delete(uid: int, conn=Depends(get_conn),
                         sess: dict = Depends(api_admin)):
        target = auth.get_user(conn, uid)
        if target is None:
            raise HTTPException(404, "user not found")
        if uid == sess["uid"]:
            raise HTTPException(400, "you can't delete your own account")
        if target["role"] == "admin" and auth.count_admins(conn) <= 1:
            raise HTTPException(400, "can't remove the last admin")
        auth.delete_user(conn, uid)
        return {"ok": True}

    @app.post("/api/users/{uid}/role")
    def api_users_role(uid: int, body: dict, conn=Depends(get_conn),
                       sess: dict = Depends(api_admin)):
        role = _norm_role(body.get("role"))
        if role is None:
            raise HTTPException(400, "role must be Admin or Basic")
        target = auth.get_user(conn, uid)
        if target is None:
            raise HTTPException(404, "user not found")
        if target["role"] == "admin" and role != "admin" \
                and auth.count_admins(conn) <= 1:
            raise HTTPException(400, "can't demote the last admin")
        auth.set_role(conn, uid, role)
        return {"ok": True}

    @app.post("/api/users/{uid}/password")
    def api_users_password(uid: int, body: dict, conn=Depends(get_conn),
                           _=Depends(api_admin)):
        if len(body.get("password") or "") < 8:
            raise HTTPException(400, "password must be at least 8 characters")
        if auth.get_user(conn, uid) is None:
            raise HTTPException(404, "user not found")
        auth.set_password(conn, uid, body["password"])
        return {"ok": True}

    # ---- station equipment monitor (ICMP ping)
    from . import devices as devmod
    pinger = devmod.Pinger(db.connect, cfg["db_path"])

    @app.on_event("startup")
    def _start_pinger():
        pinger.start()

    @app.on_event("shutdown")
    def _stop_pinger():
        pinger.stop()

    @app.get("/api/devices")
    def api_devices(conn=Depends(get_conn), _=Depends(api_user)):
        out = []
        for d in devmod.list_devices(conn):
            st = pinger.status(d["id"]) or {}
            out.append({**d, "up": st.get("up"),
                        "latency_ms": st.get("latency_ms"),
                        "checked_at": st.get("checked_at")})
        return out

    @app.post("/api/devices", status_code=201)
    def api_devices_add(body: dict, conn=Depends(get_conn), _=Depends(api_user)):
        name = (body.get("name") or "").strip()
        host = (body.get("host") or "").strip()
        if not name or not host:
            raise HTTPException(400, "give the device a name and an IP/host")
        return {"id": devmod.add(conn, name, host)}

    @app.post("/api/devices/{did}")
    def api_devices_update(did: int, body: dict, conn=Depends(get_conn),
                           _=Depends(api_user)):
        """Rename a device or correct its address (the row's Edit action).

        Additive: the add/delete/ping routes are unchanged.
        """
        name = (body.get("name") or "").strip()
        host = (body.get("host") or "").strip()
        if not name or not host:
            raise HTTPException(400, "give the device a name and an IP/host")
        if not devmod.update(conn, did, name, host):
            raise HTTPException(404, "device not found")
        return {"ok": True}

    @app.delete("/api/devices/{did}")
    def api_devices_del(did: int, conn=Depends(get_conn), _=Depends(api_user)):
        devmod.remove(conn, did)
        return {"ok": True}

    @app.post("/api/devices/{did}/ping")
    def api_devices_ping(did: int, conn=Depends(get_conn), _=Depends(api_user)):
        d = next((x for x in devmod.list_devices(conn) if x["id"] == did), None)
        if d is None:
            raise HTTPException(404, "device not found")
        up, rtt = devmod.ping(d["host"])
        return {"up": up, "latency_ms": rtt}

    _AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".aac", ".wav", ".flac", ".ogg"}

    @app.get("/api/fs/list")
    def fs_list(path: str = "", files: str = "", conn=Depends(get_conn),
                _=Depends(api_user)):
        """Browser for pickers. Always lists sub-folders; if `files` is given
        (comma list: 'audio', 'lst', or explicit exts) also lists matching
        files so you can pick a single file / .lst. Paths inside the music root
        are served from the index (fast over a VPN); everything else is live."""
        from . import playlists as pl
        want = set()
        for tok in (files or "").split(","):
            tok = tok.strip().lower()
            if tok == "audio":
                want |= _AUDIO_EXTS
            elif tok == "lst":
                want.add(".lst")
            elif tok.startswith("."):
                want.add(tok)
        if not path:
            drives = [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                      if os.path.exists(f"{c}:\\")]
            # add the browsable NAS roots so John can reach his library with no
            # Z: mapping (the picker used to show ONLY C:\ — the reported bug).
            # UNC roots render/click as folders in the picker like any drive;
            # normalise to the canonical \\host\share\... form so a click round-
            # trips cleanly back through this endpoint.
            nas = cfg.get("nas_music_root") or ""
            nas_norm = os.path.normpath(nas) if nas else ""
            for root in (pathmap.share_root(nas), nas_norm):
                if root and root not in drives:
                    drives.append(root)
            return {"path": "", "parent": None, "dirs": drives, "files": []}
        # a Z:\... path pasted into the picker still browses when Z: is gone
        path = pl.alias_path(path)
        # music library: serve from the index so browsing works over a slow VPN
        # (a live listdir of /G is thousands of SMB round-trips). .lst files
        # aren't indexed, so fall through to live when the picker wants them.
        if ".lst" not in want:
            idx = index_browse(conn, path, want, cfg.get("nas_music_root") or "")
            if idx is not None:
                return idx
        norm = os.path.abspath(path)
        if not os.path.isdir(norm):
            raise HTTPException(400, "not a folder")
        dirs, filelist = [], []
        try:
            for name in sorted(os.listdir(norm), key=str.lower):
                if name.startswith(("$", ".")):
                    continue
                full = os.path.join(norm, name)
                if os.path.isdir(full):
                    dirs.append(name)
                elif want and os.path.splitext(name)[1].lower() in want:
                    filelist.append(name)
        except OSError:
            raise HTTPException(400, "cannot read that folder")
        parent = os.path.dirname(norm.rstrip("\\/"))
        return {"path": norm, "parent": parent if parent != norm else "",
                "dirs": dirs, "files": filelist}

    @app.get("/api/backup")
    def backup(conn=Depends(get_conn), _=Depends(api_user)):
        """One-click export: every playlist + its items, as a JSON file."""
        import datetime
        import json as _json
        from fastapi.responses import Response
        from . import playlists as pl
        payload = {
            "studiofire_backup": 1,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "station": cfg["station_name"],
            "playlists": [
                {"name": p["name"],
                 "items": [{"item_type": i["item_type"], "path": i["path"],
                            "title": i["title"]}
                           for i in pl.get_items(conn, p["id"])]}
                for p in pl.list_playlists(conn)],
        }
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
        return Response(
            _json.dumps(payload, indent=1),
            media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="studiofire-backup-{stamp}.json"'})

    @app.post("/api/restore")
    def restore(file: UploadFile, conn=Depends(get_conn),
                _=Depends(api_user)):
        """Import a backup file. Existing playlists are kept; a restored
        playlist whose name is taken gets a ' (restored)' suffix."""
        import json as _json
        import sqlite3
        from . import playlists as pl
        try:
            payload = _json.loads(file.file.read())
        except ValueError:
            raise HTTPException(400, "not a valid backup file (bad JSON)")
        if (not isinstance(payload, dict)
                or payload.get("studiofire_backup") != 1
                or not isinstance(payload.get("playlists"), list)):
            raise HTTPException(400, "not a StudioFire backup file")
        imported = 0
        for p in payload["playlists"]:
            name = str(p.get("name") or "").strip()
            items = p.get("items") or []
            if not name:
                continue
            for candidate in (name, name + " (restored)"):
                try:
                    pid = pl.create_playlist(conn, candidate)
                    break
                except sqlite3.IntegrityError:
                    pid = None
            if pid is None:
                continue  # both names taken — skip, keep going
            for i in items:
                if (isinstance(i, dict) and i.get("path")
                        and i.get("item_type") in pl.ITEM_TYPES):
                    pl.add_item(conn, pid, i["item_type"], i["path"],
                                i.get("title"))
            imported += 1
        return {"ok": True, "imported": imported}

    @app.get("/api/library/search")
    def library_search(q: str = "", conn=Depends(get_conn),
                       _=Depends(api_user)):
        q = q.strip()
        if not q:
            return []
        like = f"%{q}%"
        # Include tracks currently flagged missing (marked offline) so the whole
        # indexed library — including subfolders — is findable even when a flaky
        # NAS scan wrongly flagged some. Present tracks are listed first.
        rows = conn.execute(
            "SELECT id, path, title, artist, album, duration_sec, missing "
            "FROM tracks WHERE "
            "  (title LIKE ? OR artist LIKE ? OR album LIKE ? OR path LIKE ?) "
            "ORDER BY missing ASC, artist, title LIMIT 60",
            (like, like, like, like)).fetchall()
        return [dict(r) for r in rows]

    # ---- reports: as-aired / spot playout / proof of performance
    def _report_rows(conn, start: str, end: str, kind: str) -> list[dict]:
        sql = ("SELECT ts, title, source, path FROM play_history "
               "WHERE event = 'track_start' AND substr(ts, 1, 10) BETWEEN ? "
               "AND ?")
        args = [start, end]
        if kind == "spots":
            sql += " AND source = 'spot'"
        elif kind == "music":
            sql += " AND source IN ('playlist', 'show', 'manual')"
        sql += " ORDER BY id ASC LIMIT 10000"
        out = []
        for r in conn.execute(sql, args).fetchall():
            title = r["title"] or (os.path.splitext(
                os.path.basename(r["path"]))[0] if r["path"] else "—")
            out.append({"ts": r["ts"], "title": title,
                        "source": r["source"] or ""})
        return out


    # ---- SoundExchange royalty report (Report of Use, 37 CFR 370.4) --------
    # The build runs in a worker thread against a read-only connection: it can
    # never write to the DB and never touches the playout path.
    sx_state = {"state": "idle", "started": None, "finished": None,
                "ready": None, "reasons": [], "zip": None, "files": {},
                "rows": 0, "performances": 0, "error": None, "mode": None,
                "period": None, "due": None, "out_dir": None}

    def _sx_cfg():
        from .config import load_config
        return load_config().get("royalty_reporting") or {}

    @app.get("/api/sx/config")
    def api_sx_config(_=Depends(api_user)):
        rr = _sx_cfg()
        return {"service_name": rr.get("service_name", ""),
                "filer_name": rr.get("filer_name", ""),
                "contact": rr.get("contact") or {},
                "category": rr.get("category", "B"),
                "mode": rr.get("mode", "census_atp"),
                "channel_name": rr.get("channel_name", ""),
                "output_format": rr.get("output_format", "headerless"),
                "share_aliases": rr.get("share_aliases") or [],
                "music_drive_letter": rr.get("music_drive_letter", ""),
                "configured": bool((rr.get("service_name") or "").strip()),
                "state": sx_state}

    @app.post("/api/sx/settings")
    def api_sx_settings(body: dict, _=Depends(api_admin)):
        from .config import save_royalty_reporting
        allowed = {"service_name", "filer_name", "contact", "category",
                   "mode", "channel_name", "output_format", "share_aliases",
                   "music_drive_letter"}
        block = {k: v for k, v in (body or {}).items() if k in allowed}
        if block.get("category") not in (None, "", "A", "B", "C"):
            raise HTTPException(400, "category must be A, B or C")
        if block.get("mode") not in (None, "", "census_atp", "sample_atp",
                                     "sample_ath"):
            raise HTTPException(400, "unknown reporting mode")
        if block.get("output_format") not in (None, "", "headerless",
                                             "headered"):
            raise HTTPException(400, "format must be headerless or headered")
        if isinstance(block.get("share_aliases"), str):
            block["share_aliases"] = [x.strip() for x in
                                      block["share_aliases"].split(",") if x.strip()]
        saved = save_royalty_reporting(block)
        return {"ok": True, "royalty_reporting": saved}

    def _sx_run(job: dict) -> None:
        """The background build. Never raises into the request thread."""
        import threading
        from . import sx_report as sx
        from .config import load_config
        try:
            conf = load_config()
            cfg = conf.get("royalty_reporting") or {}
            db_path = conf.get("paths", {}).get("db_path") or os.path.join(
                conf.get("paths", {}).get("data_dir", "data"), "studiofire.db")
            conn = db.connect(db_path)
            roots = list(cfg.get("share_aliases") or [])
            if not roots:
                nas = conf.get("paths", {}).get("nas_music_root")
                if nas:
                    try:
                        roots = [pathmap.share_root(nas)]
                    except Exception:      # noqa: BLE001
                        roots = [nas]
            index = sx.build_precache_index(conn, roots,
                                            cfg.get("music_drive_letter") or "")
            mode = job["mode"]
            plays = []
            for w_start, w_end in job["windows"]:
                plays.extend(sx.collect_period(conn, w_start, w_end,
                                               index=index))
            plays.sort(key=lambda r: r["start"])
            note = "no listener series supplied"
            if job.get("listeners"):
                series = sx.load_listener_series(job["listeners"])
                missing = sx.apply_listeners(plays, series)
                note = ("%d listener samples; %d performances outside their "
                        "coverage" % (len(series), missing))
            out_dir = job["out_dir"]
            result = sx.build_report(conn=conn, out_dir=out_dir, cfg=cfg,
                                     mode=mode, start=job["start"],
                                     end_excl=job["end_excl"], plays=plays,
                                     ath=job.get("ath"), extra_note=note)
            sx_state.update({
                "state": "done", "finished": time.time(),
                "ready": result["ready"], "reasons": result["reasons"],
                "zip": result["zip"], "files": result["files"],
                "rows": result["rows"], "performances": result["performances"],
                "mode": mode, "due": result["due"], "error": None,
                "period": "%s .. %s" % (job["start"].date(),
                                        (job["end_excl"] -
                                         datetime.timedelta(days=1)).date()),
                "out_dir": out_dir})
        except Exception as exc:          # noqa: BLE001 — surface, never crash
            log.exception("royalty report build failed")
            sx_state.update({"state": "error", "finished": time.time(),
                             "error": str(exc), "ready": None, "reasons": []})

    @app.post("/api/sx/build")
    def api_sx_build(body: dict, _=Depends(api_admin)):
        import threading
        from . import sx_report as sx
        if sx_state.get("state") == "running":
            raise HTTPException(409, "a report is already being built")
        mode = (body.get("mode") or _sx_cfg().get("mode") or "census_atp")
        if mode not in sx.MODES:
            raise HTTPException(400, "unknown reporting mode")
        try:
            if mode == "census_atp":
                month = (body.get("month") or "").strip()
                if not month:
                    raise HTTPException(400, "pick a month")
                start, end = sx._month_bounds(month)
                windows = [(start, end)]
            else:
                quarter = (body.get("quarter") or "").strip()
                w1, w2 = (body.get("week1") or "").strip(), \
                         (body.get("week2") or "").strip()
                (w1s, w1e), (w2s, w2e) = sx.validate_weeks(w1, w2, quarter)
                start, end = sx.quarter_bounds(quarter)
                windows = [(w1s, w1e), (w2s, w2e)]
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except HTTPException:
            raise
        except Exception as exc:          # noqa: BLE001
            raise HTTPException(400, "could not work out the period: %s" % exc)
        ath = body.get("ath")
        if mode == "sample_ath":
            try:
                ath = float(ath)
            except (TypeError, ValueError):
                raise HTTPException(400, "aggregate tuning hours is required "
                                         "for the listening-hours format")
        from .config import load_config
        data_dir = load_config().get("paths", {}).get("data_dir", "data")
        out_dir = os.path.join(data_dir, "sx_reports",
                               datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        listeners = body.get("listeners_path") or ""
        if listeners and not os.path.isfile(listeners):
            listeners = ""
        job = {"mode": mode, "start": start, "end_excl": end,
               "windows": windows, "ath": ath, "listeners": listeners,
               "out_dir": out_dir}
        sx_state.update({"state": "running", "started": time.time(),
                         "finished": None, "ready": None, "reasons": [],
                         "error": None, "zip": None, "files": {}, "mode": mode,
                         "out_dir": out_dir, "due": None, "period": None})
        threading.Thread(target=_sx_run, args=(job,), daemon=True).start()
        return {"ok": True, "state": sx_state}

    @app.get("/api/sx/status")
    def api_sx_status(_=Depends(api_admin)):
        return sx_state

    @app.post("/api/sx/listeners")
    def api_sx_listeners(file: UploadFile, _=Depends(api_admin)):
        """Store an uploaded listener series CSV and hand back its path."""
        from .config import load_config
        data_dir = load_config().get("paths", {}).get("data_dir", "data")
        folder = os.path.join(data_dir, "sx_listeners")
        os.makedirs(folder, exist_ok=True)
        name = os.path.basename(file.filename or "listeners.csv")
        safe = "".join(c for c in name if c.isalnum() or c in "._-") or "listeners.csv"
        path = os.path.join(folder, datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + safe)
        with open(path, "wb") as fh:
            fh.write(file.file.read())
        return {"ok": True, "path": path}

    @app.get("/api/sx/download")
    def api_sx_download(_=Depends(api_admin)):
        zip_path = sx_state.get("zip")
        if not zip_path or not os.path.isfile(zip_path):
            raise HTTPException(404, "no report has been built yet")
        return FileResponse(zip_path, media_type="application/zip",
                            filename=os.path.basename(zip_path))

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page(request: Request, sess: dict = Depends(page_user)):
        cat = reports.catalog()
        return render(request, "reports.html", role=sess["role"],
                      reports=cat, reports_json=json.dumps(cat))

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request, sess: dict = Depends(page_user),
                  conn=Depends(get_conn)):
        return render(request, "help.html", role=sess["role"],
                      running_version=running_version,
                      support_contact=db.get_setting(conn, "support_contact")
                      or "",
                      changelog=changelog.load())

    @app.get("/api/reports")
    def api_reports(start: str = "", end: str = "", kind: str = "all",
                    conn=Depends(get_conn), _=Depends(api_user)):
        start = start or datetime.date.today().isoformat()
        end = end or start
        rows = _report_rows(conn, start, end, kind)
        return {"start": start, "end": end, "kind": kind,
                "count": len(rows), "rows": rows}

    @app.get("/api/reports.csv")
    def api_reports_csv(start: str = "", end: str = "", kind: str = "all",
                        conn=Depends(get_conn), _=Depends(api_user)):
        start = start or datetime.date.today().isoformat()
        end = end or start
        rows = _report_rows(conn, start, end, kind)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Time", "What aired", "Type"])
        for r in rows:
            w.writerow([r["ts"], r["title"], r["source"]])
        fname = f"studiofire-aslog-{start}_to_{end}-{kind}.csv"
        return Response(content=buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{fname}"'})

    def _report_range(start: str, end: str) -> tuple[str, str]:
        """Validate + clamp a report window (never trust the query string)."""
        today = datetime.date.today()
        try:
            s = datetime.date.fromisoformat(start) if start else today
        except ValueError:
            s = today
        try:
            e = datetime.date.fromisoformat(end) if end else s
        except ValueError:
            e = s
        if e < s:
            s, e = e, s
        if (e - s).days > 400:          # a report is evidence, not a data dump
            s = e - datetime.timedelta(days=400)
        return s.isoformat(), e.isoformat()

    @app.get("/api/reports/{report_id}")
    def api_report_run(report_id: str, start: str = "", end: str = "",
                       conn=Depends(get_conn), _=Depends(api_user)):
        """Run one report and return it as data (screen tables read this)."""
        s, e = _report_range(start, end)
        try:
            return reports.run(report_id, conn, s, e)
        except ValueError:
            raise HTTPException(404, "unknown report")

    @app.get("/api/reports/{report_id}.csv")
    def api_report_csv(report_id: str, start: str = "", end: str = "",
                       conn=Depends(get_conn), _=Depends(api_user)):
        s, e = _report_range(start, end)
        try:
            rep = reports.run(report_id, conn, s, e)
        except ValueError:
            raise HTTPException(404, "unknown report")
        body = reports.to_csv(rep)
        station = str(cfg.get("station_name") or "StudioFire")
        slug = "".join(c if c.isalnum() else "-" for c in station).strip("-")
        fname = f"{slug or 'StudioFire'}-{report_id}-{s}_to_{e}.csv"
        return Response(content=body, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{fname}"'})

    @app.get("/reports/print", response_class=HTMLResponse)
    def report_print(request: Request, report: str = "as-aired",
                     start: str = "", end: str = "",
                     sess: dict = Depends(page_user),
                     conn=Depends(get_conn)):
        """A print-ready page: the browser's own Print → Save as PDF is the
        station's PDF path (no Chrome dependency on the playout box)."""
        s, e = _report_range(start, end)
        try:
            rep = reports.run(report, conn, s, e)
        except ValueError:
            raise HTTPException(404, "unknown report")
        return render(request, "report_print.html", role=sess["role"],
                      rep=rep, running_version=running_version,
                      generated=datetime.datetime.now().strftime(
                          "%d %B %Y, %H:%M"))

    @app.get("/api/health/tiles")
    def health_tiles(conn=Depends(get_conn), _=Depends(api_user)):
        import json as _json
        import shutil as _shutil
        tiles = []
        from . import playlists as pl
        nas = cfg.get("nas_music_root") or ""
        # resolve so a station that (mis)configured nas_music_root as a Z:\ path
        # still reports Connected when the share is reachable by UNC
        nas_ok = bool(nas) and os.path.isdir(pl.alias_path(nas))
        tiles.append({"name": "Music library (NAS)",
                      "state": "green" if nas_ok else "red",
                      "detail": "Connected" if nas_ok else
                                ("Not configured" if not nas
                                 else "UNREACHABLE — playing from cache")})
        try:
            du = _shutil.disk_usage(cfg.get("data_dir", "."))
            free_gb = du.free / 1e9
            state = ("green" if free_gb > 10
                     else "yellow" if free_gb > 2 else "red")
            tiles.append({"name": "Disk space", "state": state,
                          "detail": f"{free_gb:.0f} GB free"})
        except OSError:
            tiles.append({"name": "Disk space", "state": "yellow",
                          "detail": "Could not check"})
        raw = db.get_setting(conn, "indexer_status")
        if raw:
            try:
                s = _json.loads(raw)
                n = conn.execute("SELECT COUNT(*) FROM tracks "
                                 "WHERE missing = 0").fetchone()[0]
                detail = f"{n} tracks"
                if s.get("state") == "scanning":
                    if s.get("phase") == "tags":
                        left = s.get("tags_left")
                        detail += (f" — reading tags ({left:,} left)"
                                   if left else " — reading tags…")
                    else:
                        detail += " — finding files…"
                tiles.append({"name": "Library index", "state": "green",
                              "detail": detail})
            except ValueError:
                pass
        else:
            tiles.append({"name": "Library index", "state": "yellow",
                          "detail": "Indexer has not run yet"})
        devs = devmod.list_devices(conn)
        if devs:
            checked = [pinger.status(d["id"]) for d in devs]
            up = sum(1 for s in checked if s and s.get("up"))
            unknown = sum(1 for s in checked if not s)
            state = ("green" if up == len(devs)
                     else "yellow" if (up or unknown) else "red")
            detail = f"{up}/{len(devs)} reachable"
            if unknown:
                detail += " (checking…)"
            tiles.append({"name": "Station equipment", "state": state,
                          "detail": detail})
        return tiles

    # ---- software updates (GitHub Releases; see services/updater.py)
    import threading as _threading
    upd = {"check": None}
    _upd_stop = _threading.Event()
    UPDATE_CHECK_EVERY = 6 * 3600

    def _update_checker():
        # first check shortly after startup, then every few hours. Offline
        # boxes just record the error — never affects anything else.
        if _upd_stop.wait(30):
            return
        while True:
            try:
                upd["check"] = updater.check()
            except Exception:  # noqa: BLE001 — a check must never crash P2
                log.exception("update check failed")
            if _upd_stop.wait(UPDATE_CHECK_EVERY):
                return

    @app.on_event("startup")
    def _start_update_checker():
        if cfg.get("update_check", False):   # load_config turns it on
            _threading.Thread(target=_update_checker, name="update-check",
                              daemon=True).start()

    @app.on_event("shutdown")
    def _stop_update_checker():
        _upd_stop.set()

    def _update_view(sess: dict) -> dict:
        chk = upd["check"] or {}
        return {"current": running_version,
                "latest": chk.get("latest"),
                "update_available": bool(chk.get("update_available")),
                "checked_at": chk.get("checked_at"),
                "check_error": chk.get("error"),
                "dev_checkout": updater.is_dev_checkout(),
                "can_install": sess["role"] == "admin",
                # §6.3 major-version gate — DETECT + INFORM ONLY. This 1.x build
                # exposes NO way to install a major (no button, no endpoint; see
                # updater.MAJOR_INSTALL_ENABLED). A higher major is surfaced to
                # admins as a muted notice so they know to contact VeteranOp
                # before upgrading. major_available is data only; can_major is
                # just admin-visibility of that notice. The full admin major-
                # install experience is deferred to a later release (after 2.0
                # is proven on a home-PC 72-hour burn-in).
                "major_available": chk.get("major_available"),
                "can_major": sess["role"] == "admin",
                "state": updater.read_state()}

    @app.get("/api/update")
    def api_update(sess: dict = Depends(api_user)):
        return _update_view(sess)

    @app.post("/api/update/check")
    def api_update_check(sess: dict = Depends(api_user)):
        upd["check"] = updater.check()
        return _update_view(sess)

    # NOTE (§6.3 scope, 2026-10-05): there is intentionally NO
    # /api/update/install-major (or pre-flight) endpoint in the 1.x line. The
    # gate here is purely defensive — detect a major, inform an admin, and never
    # expose a way to install one. The everyday /api/update/install below only
    # ever targets the within-major release (check()'s update_available), so it
    # cannot cross a major even if asked. The full admin major-install
    # experience is deferred to a later release (after 2.0 is proven on a
    # home-PC 72-hour burn-in).

    @app.post("/api/update/install")
    def api_update_install(sess: dict = Depends(api_admin)):
        """Start installing the latest release in a detached helper (this
        web service is one of the things it restarts). Admin only."""
        if updater.is_dev_checkout():
            raise HTTPException(409, "this is a developer (git) checkout — "
                                     "update it with git")
        chk = upd["check"] = updater.check()
        if chk.get("error"):
            raise HTTPException(502, chk["error"])
        if not chk.get("update_available"):
            raise HTTPException(409, "already up to date")
        busy = updater.read_state()
        if busy.get("state") in ("starting", "downloading", "backing_up",
                                 "installing", "restarting", "rolling_back") \
                and time.time() - (busy.get("updated_at") or 0) < 45 * 60:
            raise HTTPException(409, "an update is already running")
        updater.spawn_detached_apply(chk["latest"]["tag"])
        log.warning("GUI-triggered update %s -> %s", running_version,
                    chk["latest"]["version"])
        return {"ok": True, "to": chk["latest"]["version"]}

    @app.get("/schedule", response_class=HTMLResponse)
    def schedule_calendar_page(request: Request, sess: dict = Depends(page_user)):
        return render(request, "schedule.html", role=sess["role"])

    @app.get("/api/calendar")
    def api_calendar(month: str = "", conn=Depends(get_conn),
                     _=Depends(api_user)):
        """A month of scheduled playlists/shows for the calendar. Only shows
        (scheduled playlists, files, folders, .lst) — not spots — resolved
        against each date."""
        import calendar as _cal
        today = datetime.date.today()
        try:
            y, m = (int(x) for x in month.split("-"))
            datetime.date(y, m, 1)
        except (ValueError, TypeError, AttributeError):
            y, m = today.year, today.month
        base = None
        bpid = db.get_setting(conn, "active_playlist_id")
        if bpid:
            row = conn.execute("SELECT name FROM playlists WHERE id = ?",
                               (int(bpid),)).fetchone()
            base = row["name"] if row else None
        days = []
        for d in range(1, _cal.monthrange(y, m)[1] + 1):
            date = datetime.date(y, m, d)
            days.append({"day": d, "weekday": date.weekday(),
                         "today": date == today,
                         "shows": sched.occurrences_on(conn, date)})
        return {"year": y, "month": m, "month_name": _cal.month_name[m],
                "first_weekday": datetime.date(y, m, 1).weekday(),
                "base": base, "days": days}

    from . import duplicates, engine_bridge, playlists, streaming
    playlists.register(app)
    engine_bridge.register(app)
    duplicates.register(app)
    streaming.register(app)      # Settings > Streaming: config + live monitor

    return app
