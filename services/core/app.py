"""P2 FastAPI application factory.

Pages are server-rendered Jinja2 (+ HTMX later, task: web GUI). This module
owns the app skeleton: sessions, first-run setup, login/logout, and the
auth dependencies every later router builds on.
"""

from __future__ import annotations

import logging
import os

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db, spots

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(ROOT, "web")
log = logging.getLogger("core.app")


def create_app(cfg: dict) -> FastAPI:
    db.migrate(cfg["db_path"])
    sessions = auth.Sessions(auth.load_secret(cfg["secret_path"]))
    templates = Jinja2Templates(directory=os.path.join(WEB, "templates"))
    app = FastAPI(title="StudioFire", docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.sessions = sessions
    app.state.templates = templates
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

    def render(request, name, **ctx):
        ctx.setdefault("station", cfg["station_name"])
        return templates.TemplateResponse(request, name, ctx)

    # ------------------------------------------------------------- routes

    @app.get("/health")
    def health():
        return {"ok": True, "service": "core"}

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, conn=Depends(get_conn)):
        if auth.any_users(conn):
            return RedirectResponse("/login", status_code=303)
        return render(request, "setup.html")

    @app.post("/setup")
    def setup_submit(request: Request, conn=Depends(get_conn),
                     username: str = Form(...), password: str = Form(...),
                     password2: str = Form(...)):
        if auth.any_users(conn):  # setup runs exactly once
            return RedirectResponse("/login", status_code=303)
        if not username.strip() or len(password) < 8 or password != password2:
            return render(request, "setup.html",
                          error="Passwords must match and be at least "
                                "8 characters.")
        uid = auth.create_user(conn, username.strip(), password, "admin")
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(auth.SESSION_COOKIE, sessions.issue(uid, "admin"),
                        httponly=True, samesite="lax",
                        max_age=auth.SESSION_MAX_AGE)
        log.info("first-run setup: admin %r created", username.strip())
        return resp

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
        return render(request, "dashboard.html", role=sess["role"])

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
    def settings_page(request: Request, sess: dict = Depends(page_user)):
        if sess["role"] != "admin":
            return RedirectResponse("/", status_code=303)
        return render(request, "settings.html", role=sess["role"])

    @app.get("/api/settings/dirs")
    def get_dirs(conn=Depends(get_conn), _=Depends(api_admin)):
        return [{"key": k, "label": lbl, "hint": hint,
                 "path": db.get_setting(conn, k) or "",
                 "exists": os.path.isdir(db.get_setting(conn, k) or "")}
                for k, lbl, hint in DIR_SETTINGS]

    @app.post("/api/settings/dirs")
    def set_dir(body: dict, conn=Depends(get_conn), _=Depends(api_admin)):
        key, path = body.get("key"), (body.get("path") or "").strip()
        if key not in {k for k, _, _ in DIR_SETTINGS}:
            raise HTTPException(400, "unknown setting")
        if path and not os.path.isdir(path):
            raise HTTPException(400, "that folder does not exist")
        db.set_setting(conn, key, path)
        return {"ok": True}

    @app.get("/api/fs/list")
    def fs_list(path: str = "", _=Depends(api_admin)):
        """Folder browser for the settings page (directories only)."""
        if not path:
            drives = [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                      if os.path.exists(f"{c}:\\")]
            return {"path": "", "parent": None, "dirs": drives}
        norm = os.path.abspath(path)
        if not os.path.isdir(norm):
            raise HTTPException(400, "not a folder")
        dirs = []
        try:
            for name in sorted(os.listdir(norm), key=str.lower):
                if name.startswith(("$", ".")):
                    continue
                if os.path.isdir(os.path.join(norm, name)):
                    dirs.append(name)
        except OSError:
            raise HTTPException(400, "cannot read that folder")
        parent = os.path.dirname(norm.rstrip("\\/"))
        return {"path": norm,
                "parent": parent if parent != norm else "",
                "dirs": dirs}

    @app.get("/api/backup")
    def backup(conn=Depends(get_conn), _=Depends(api_admin)):
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
                _=Depends(api_admin)):
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
        rows = conn.execute(
            "SELECT id, path, title, artist, album, duration_sec "
            "FROM tracks WHERE missing = 0 AND "
            "  (title LIKE ? OR artist LIKE ? OR album LIKE ? OR path LIKE ?) "
            "ORDER BY artist, title LIMIT 50",
            (like, like, like, like)).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/health/tiles")
    def health_tiles(conn=Depends(get_conn), _=Depends(api_user)):
        import json as _json
        import shutil as _shutil
        tiles = []
        nas = cfg.get("nas_music_root") or ""
        nas_ok = bool(nas) and os.path.isdir(nas)
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
                tiles.append({"name": "Library index",
                              "state": "green",
                              "detail": (f"{n} tracks"
                                         + (" — indexing…"
                                            if s.get("state") == "scanning"
                                            else ""))})
            except ValueError:
                pass
        else:
            tiles.append({"name": "Library index", "state": "yellow",
                          "detail": "Indexer has not run yet"})
        return tiles

    from . import engine_bridge, playlists
    playlists.register(app)
    engine_bridge.register(app)

    return app
