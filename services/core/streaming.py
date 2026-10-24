"""Streaming settings + live metadata monitor (#747).

Two jobs, one page section:

1. **Configuration** — where the now-playing push goes (target URL, mount,
   credential, on/off). Values live in the DB `settings` table so an operator can
   change them from the GUI; the *credential* never does: it goes to
   `data/stream_secret.json`, which is gitignored, is excluded from the release
   payload, and is never sent back to the browser (write-only field).

2. **Monitoring** — what the station *sent*, and what a listener *receives*.
   Those are different questions and both are answered here:
   * the pusher's own last attempt/title/result (in-process state), and
   * a live probe of the mount: `status-json.xsl` for the server's view plus a
     real ICY read for the listener's view.

Design rules carried over from the pusher:
  * P2 only, never the audio path. Every probe is bounded by a short timeout and
    wrapped: a stream host being slow or down must never affect playout.
  * Nothing here starts a network push unless an operator enabled it AND a
    credential exists — an empty config = DryRunTransport, exactly as before.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import Depends, HTTPException

from . import db
from .nowplaying_push import DryRunTransport, NowPlayingPusher

log = logging.getLogger("core.streaming")

# DB settings keys (non-secret). Prefixed so they are easy to find by hand.
K_ENABLED = "stream_enabled"        # "1" / "0"
K_ENDPOINT = "stream_endpoint"      # http://host:port
K_MOUNT = "stream_mount"            # /live
K_TRANSPORT = "stream_transport"    # "icecast" (only transport supported)

SECRET_FILE = "stream_secret.json"
MASK = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"


# ------------------------------------------------------------------ settings

def _data_dir(cfg: dict) -> str:
    """config.load_config() exposes `data_dir` at the TOP level (there is no
    cfg["paths"] in the loaded dict — that is the on-disk JSON shape, not the
    loaded one). Resolve it the same way the rest of core does, or the secret
    lands in a relative `data/` next to whatever the process CWD happens to be."""
    return (cfg.get("data_dir")
            or (cfg.get("paths") or {}).get("data_dir")
            or os.path.join("data"))


def secret_path(cfg: dict) -> str:
    return os.path.join(_data_dir(cfg), SECRET_FILE)


def load_secret(cfg: dict) -> dict:
    """{"user": str, "password": str}; empty strings when unset/unreadable."""
    path = secret_path(cfg)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {"user": str(data.get("user") or ""),
                "password": str(data.get("password") or "")}
    except (OSError, ValueError):
        return {"user": "", "password": ""}


def save_secret(cfg: dict, user: str, password: str) -> None:
    """Write the credential atomically. Called only when the operator actually
    typed something — an untouched (masked/blank) password field leaves the
    stored secret alone, so a save never silently wipes a working credential."""
    path = secret_path(cfg)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"user": user, "password": password}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_settings(conn) -> dict:
    """The operator-facing config, with defaults filled in."""
    return {
        "enabled": (db.get_setting(conn, K_ENABLED) or "0") == "1",
        "endpoint": db.get_setting(conn, K_ENDPOINT) or "",
        "mount": db.get_setting(conn, K_MOUNT) or "/live",
        "transport": db.get_setting(conn, K_TRANSPORT) or "icecast",
    }


def _validate(endpoint: str, mount: str) -> None:
    if endpoint:
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(400, "target URL must look like "
                                     "http://host:port (no path)")
    if mount and not mount.startswith("/"):
        raise HTTPException(400, "mount must start with / (e.g. /live)")


def merged_np_config(cfg: dict, conn, secret: dict | None = None) -> dict:
    """What NowPlayingPusher gets: config.json defaults, DB values on top, then
    the stored credential. Kept in one place so the GUI, the startup path and
    the tests can never disagree about precedence."""
    base = dict(cfg.get("nowplaying_push") or {})
    st = read_settings(conn)
    out = dict(base)
    out["enabled"] = bool(st["enabled"])
    out["endpoint"] = st["endpoint"] or base.get("endpoint", "")
    out["mount"] = st["mount"] or base.get("mount", "")
    out["transport"] = st["transport"] or base.get("transport", "icecast")
    sec = secret if secret is not None else load_secret(cfg)
    if sec.get("user") or sec.get("password"):
        out["auth"] = f"{sec.get('user', '')}:{sec.get('password', '')}"
    out.setdefault("timeout_sec", 4.0)
    out.setdefault("min_interval_sec", 5.0)
    out.setdefault("include_spots", False)
    return out


def configured(cfg: dict, conn) -> bool:
    """True only when a push could actually happen: enabled, endpoint+mount, and
    a credential (DB/env/file). Anything less = silent DryRun, as designed."""
    np = merged_np_config(cfg, conn)
    return bool(np.get("enabled") and np.get("endpoint") and np.get("mount")
                and np.get("auth"))


# ------------------------------------------------------------------- reload

def reload_pusher(app, conn=None) -> dict:
    """Swap in a pusher built from the CURRENT settings, without a restart.

    The feeder loop reads `app.state.nowplaying_pusher` on every tick, so the
    swap is picked up on the next tick; the old worker is stopped first so two
    pushers never push at once."""
    old = getattr(app.state, "nowplaying_pusher", None)
    try:
        if old is not None:
            old.stop()
    except Exception:
        log.warning("stopping the old nowplaying pusher failed", exc_info=True)

    close = False
    if conn is None:
        # app.state.get_conn is a FastAPI *generator* dependency: call db.connect
        # directly rather than next()-ing it by hand
        conn = db.connect(app.state.cfg["db_path"])
        close = True
    try:
        np_cfg = merged_np_config(app.state.cfg, conn)
        ready = configured(app.state.cfg, conn)
    finally:
        if close:
            conn.close()

    pusher = NowPlayingPusher(np_cfg)
    app.state.nowplaying_pusher = pusher
    pusher.start()
    log.info("nowplaying pusher reloaded (enabled=%s transport=%s endpoint=%r "
             "mount=%r auth=%s)", np_cfg.get("enabled"),
             np_cfg.get("transport"), np_cfg.get("endpoint"),
             np_cfg.get("mount"), "set" if np_cfg.get("auth") else "MISSING")
    return {"enabled": bool(np_cfg.get("enabled")), "configured": ready,
            "transport": type(pusher.transport).__name__}


# -------------------------------------------------------------- live monitor

_PROBE_CACHE: dict = {}
PROBE_TTL_SEC = 5.0


def probe_mount(endpoint: str, mount: str, timeout: float = 2.5) -> dict:
    """What the stream host says and what a listener actually receives.

    Two independent reads, because they disagree in interesting ways:
      * `status-json.xsl` = the server's own record (listeners, current title),
      * the ICY metadata block = the title a player will display. Read FOUR
        blocks: Icecast writes the title at the next 16 kB boundary, so the
        first block after a change is routinely stale (this produced a false
        "nothing is published" on 2026-10-24).
    """
    if not endpoint:
        return {"ok": False, "error": "no target URL configured"}
    key = (endpoint.rstrip("/"), mount)
    now = time.time()
    hit = _PROBE_CACHE.get(key)
    if hit and (now - hit[0]) < PROBE_TTL_SEC:
        return hit[1]

    out = {"ok": False, "endpoint": endpoint, "mount": mount,
           "server_title": None, "icy_title": None, "listeners": None,
           "server_name": None, "error": None,
           "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    base = endpoint.rstrip("/")
    try:
        with urllib.request.urlopen(base + "/status-json.xsl",
                                    timeout=timeout) as r:
            data = json.load(r)
        src = data.get("icestats", {}).get("source") or {}
        if isinstance(src, list):          # multi-mount servers
            src = next((s for s in src
                        if s.get("listenurl", "").endswith(mount)), {})
        out["server_title"] = src.get("title") or None
        out["listeners"] = src.get("listeners")
        out["server_name"] = src.get("server_name")
        out["ok"] = True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["error"] = f"status page unreachable: {exc}"

    try:
        req = urllib.request.Request(
            base + mount, headers={"Icy-MetaData": "1",
                                   "User-Agent": "StudioFire-monitor/1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            metaint = int(r.headers.get("icy-metaint", "0") or 0)
            if metaint:
                for _ in range(4):
                    r.read(metaint)
                    ln = r.read(1)[0] * 16
                    block = r.read(ln).decode("utf-8", "replace") if ln else ""
                    if "StreamTitle=" in block:
                        raw = block.split("StreamTitle=", 1)[1]
                        raw = raw.split(";", 1)[0].strip().strip("'")
                        if raw:
                            out["icy_title"] = raw
                            out["ok"] = True
                            break
            else:
                out["error"] = out["error"] or "this mount carries no metadata"
    except (urllib.error.URLError, OSError) as exc:
        out["error"] = out["error"] or f"stream unreachable: {exc}"

    _PROBE_CACHE[key] = (now, out)
    return out


def status(app, conn) -> dict:
    """Everything the monitor panel shows in one payload."""
    cfg = app.state.cfg
    st = read_settings(conn)
    pusher = getattr(app.state, "nowplaying_pusher", None)
    sec = load_secret(cfg)
    engine = getattr(app.state, "engine", None)
    now_playing = None
    try:
        now_playing = (engine.status() or {}).get("now_playing") if engine else None
    except Exception:
        now_playing = None
    return {
        "enabled": st["enabled"],
        "endpoint": st["endpoint"],
        "mount": st["mount"],
        "transport": st["transport"],
        "user": sec.get("user") or "",
        "credential_set": bool(sec.get("user") or sec.get("password")),
        "configured": configured(cfg, conn),
        "push": (pusher.status() if pusher is not None else None),
        "on_air_path": now_playing,
        "probe": (probe_mount(st["endpoint"], st["mount"] or "/live", 2.5)
                  if st["endpoint"]
                  else {"ok": False, "error": "no target URL configured"}),
    }


# ------------------------------------------------------------------- routes

def register(app) -> None:
    api_user = app.state.api_user
    api_admin = app.state.api_admin
    get_conn = app.state.get_conn

    @app.get("/api/streaming/status")
    def api_streaming_status(conn=Depends(get_conn), _=Depends(api_user)):
        return status(app, conn)

    @app.post("/api/streaming/settings")
    def api_streaming_save(body: dict, conn=Depends(get_conn),
                           _=Depends(api_admin)):
        endpoint = (body.get("endpoint") or "").strip().rstrip("/")
        mount = (body.get("mount") or "/live").strip()
        _validate(endpoint, mount)
        db.set_setting(conn, K_ENDPOINT, endpoint)
        db.set_setting(conn, K_MOUNT, mount)
        db.set_setting(conn, K_TRANSPORT, "icecast")
        db.set_setting(conn, K_ENABLED, "1" if body.get("enabled") else "0")
        # credential: only touched when the operator actually typed one
        user = (body.get("user") or "").strip()
        password = body.get("password") or ""
        if user or password:
            if not user or not password:
                raise HTTPException(400, "set both the username and the "
                                         "password, or leave both blank")
            save_secret(app.state.cfg, user, password)
        res = reload_pusher(app, conn)
        log.info("streaming settings saved: enabled=%s endpoint=%r mount=%r "
                 "credential=%s", bool(body.get("enabled")), endpoint, mount,
                 "updated" if (user or password) else "unchanged")
        return {"ok": True, **res}

    @app.post("/api/streaming/test")
    def api_streaming_test(conn=Depends(get_conn), _=Depends(api_admin)):
        """Send one test title through the CONFIGURED transport and report what
        happened. This is the operator's 'does it work' button — it uses the
        same code path as a real push, so a success here means a success on
        air."""
        pusher = getattr(app.state, "nowplaying_pusher", None)
        if pusher is None:
            raise HTTPException(409, "the metadata pusher is not running")
        tr = pusher.transport
        if isinstance(tr, DryRunTransport):
            return {"ok": False, "sent": False,
                    "detail": "Not configured — save a target URL and a "
                              "username/password first. Nothing was sent."}
        title = "StudioFire - Metadata test"
        try:
            tr.send({"song": title}, 5.0)
        except Exception as exc:                       # network/auth failure
            pusher.note_test(title, False, f"{type(exc).__name__}: {exc}")
            return {"ok": False, "sent": True, "detail": f"{type(exc).__name__}: {exc}"}
        pusher.note_test(title, True)
        st = read_settings(conn)
        probe = probe_mount(st["endpoint"], st["mount"] or "/live", 2.5)
        return {"ok": True, "sent": True, "title": title, "mount": probe}
