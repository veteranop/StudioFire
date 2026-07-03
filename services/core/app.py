"""P2 FastAPI application factory.

Pages are server-rendered Jinja2 (+ HTMX later, task: web GUI). This module
owns the app skeleton: sessions, first-run setup, login/logout, and the
auth dependencies every later router builds on.
"""

from __future__ import annotations

import logging
import os

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, db

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

    from . import playlists
    playlists.register(app)

    return app
