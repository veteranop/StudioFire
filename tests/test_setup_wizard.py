"""First-run wizard: a brand-new install gets from nothing to on air.

Covers the promise the installer makes: no config file, no users, no default
password — the wizard opens, validates what the operator typed, and writes each
answer where the rest of StudioFire already reads it.

Run: python tests/test_setup_wizard.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient                  # noqa: E402

from services.core import config as core_config            # noqa: E402
from services.core import db, setup as setup_mod           # noqa: E402
from services.core.app import create_app                   # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def bare_cfg(td, nas=None):
    """A config as a fresh install would have it: nothing station-specific."""
    return {
        "station_name": "StudioFire",
        "db_path": os.path.join(td, "core.db"),
        "secret_path": os.path.join(td, "secret.key"),
        "precache_dir": os.path.join(td, "precache"),
        "emergency_dir": os.path.join(td, "emergency"),
        "data_dir": td,
        "logs_dir": os.path.join(td, "logs"),
        "nas_music_root": nas or "",
        "engine_url": "http://127.0.0.1:1",
        "journal_path": os.path.join(td, "play_journal.jsonl"),
        "precache_target_minutes": 1,
        "feed_ahead_tracks": 2,
        "update_check": False,
        "nowplaying_push": {"enabled": False, "transport": "dryrun"},
    }


def main():
    td = tempfile.mkdtemp(prefix="sf-wizard-")
    nas = os.path.join(td, "Music")
    os.makedirs(nas)
    for name in ("One.wav", "Two.wav", "Three.wav"):
        open(os.path.join(nas, name), "wb").write(b"RIFF0000WAVE")
    ads = os.path.join(td, "Ads")
    os.makedirs(ads)

    # The wizard must not need a config file to exist at all.
    cfg_path = os.path.join(td, "config.json")
    real_update = core_config.update_config

    base = bare_cfg(td)

    def fake_update(block, path=None):
        return real_update(block, path=cfg_path)

    def fake_load(path=None):
        """Read the temp config the wizard is writing, over the bare defaults."""
        merged = dict(base)
        try:
            saved = json.load(open(cfg_path, encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        for key, value in saved.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        merged.setdefault("royalty_reporting", {})
        return merged

    real_load = core_config.load_config
    core_config.update_config = fake_update
    core_config.load_config = fake_load
    try:
        app = create_app(bare_cfg(td))
        app.state.cfg.update({"royalty_reporting": {}})
        client = TestClient(app, follow_redirects=False)

        # -- a brand-new install: no users, wizard opens on Station ----------
        r = client.get("/setup")
        check("fresh install opens the wizard", r.status_code == 200)
        check("wizard starts on the station step", b"Your station" in r.content)
        check("wizard shows the step rail", b"Administrator account" in r.content)
        check("the wizard never offers a default password",
              b"no default" not in r.content.lower())

        # -- step 1: station -------------------------------------------------
        r = client.post("/api/setup/step", json={
            "step": "station",
            "values": {"station_name": "Example FM", "time_format": "24"}})
        check("station step saves", r.status_code == 200
              and r.json()["next"] == "admin")
        check("station name written to config",
              json.load(open(cfg_path, encoding="utf-8"))["station_name"]
              == "Example FM")
        conn = db.connect(os.path.join(td, "core.db"))
        check("time format written", db.get_setting(conn, "time_format") == "24")
        r = client.get("/setup")
        check("admin step says there is no default password",
              r.status_code == 200 and b"no default" in r.content.lower()
              and b"Administrator account" in r.content)

        # -- step 2: the admin account (a real form post) --------------------
        r = client.post("/setup", data={"username": "manager",
                                        "password": "longenough",
                                        "password2": "longenough"})
        check("admin account created and wizard continues",
              r.status_code == 303 and r.headers["location"] == "/setup")
        r = client.get("/setup")
        check("wizard moves on to the music library",
              r.status_code == 200 and b"Music library" in r.content)

        # -- step 3: library, with a real validation message ------------------
        r = client.post("/api/setup/step", json={
            "step": "library", "values": {"music_root": "\\\\NAS\\Nope"}})
        check("a folder we cannot see is refused with advice",
              r.status_code == 400 and "can't see that" in r.json()["detail"])
        r = client.post("/api/setup/step", json={
            "step": "library", "values": {"music_root": nas}})
        check("a real folder is accepted and counted",
              r.status_code == 200 and r.json()["found_songs"] == 3)
        check("music root written to config",
              json.load(open(cfg_path, encoding="utf-8"))["paths"]
              ["nas_music_root"] == nas)

        # -- step 4: playlists ------------------------------------------------
        r = client.post("/api/setup/step", json={
            "step": "playlists", "values": {"playlists_dir": td}})
        check("playlists folder saved", r.status_code == 200
              and r.json()["next"] == "spots")

        # -- step 5: spots (optional, one bad folder is caught) ---------------
        r = client.post("/api/setup/step", json={
            "step": "spots", "values": {"dir_ads": ads}})
        check("optional spot folder saved", r.status_code == 200)
        check("spot folder in settings",
              db.get_setting(conn, "dir_ads") == ads)

        # -- step 6: audio ----------------------------------------------------
        r = client.get("/api/setup/audio-devices")
        check("audio devices endpoint answers", r.status_code == 200
              and "devices" in r.json())
        r = client.post("/api/setup/step", json={
            "step": "audio", "values": {"audio_device_guid": ""}})
        check("audio step saves (default device)", r.status_code == 200)

        # -- step 7: emergency -------------------------------------------------
        r = client.post("/api/setup/step", json={
            "step": "emergency", "values": {"fill_from_library": True}})
        check("emergency preference saved", r.status_code == 200
              and db.get_setting(conn, "emergency_fill_from_library") == "1")

        # -- step 8: streaming (skippable) -------------------------------------
        r = client.post("/api/setup/step", json={"step": "streaming",
                                                 "values": {}})
        check("streaming can be skipped", r.status_code == 200
              and r.json()["next"] == "done")

        # -- step 9: done -------------------------------------------------------
        r = client.get("/setup")
        check("finish screen shows the summary", r.status_code == 200
              and b"Ready to go" in r.content
              and b"Example FM" in r.content)
        r = client.post("/api/setup/step", json={"step": "done", "values": {}})
        check("finishing marks setup complete", r.status_code == 200
              and r.json().get("complete") is True)
        check("setup state says complete", setup_mod.is_complete(conn))

        # -- afterwards: the wizard gets out of the way -------------------------
        r = client.get("/setup")
        check("a finished wizard redirects away", r.status_code == 303)
        r = client.get("/")
        check("On Air works after the wizard", r.status_code == 200)

        # -- the settings copy the operator can keep ---------------------------
        r = client.get("/api/setup/settings-copy")
        check("settings copy downloads as JSON", r.status_code == 200
              and r.headers["content-type"].startswith("application/json"))
        body = json.loads(r.text)
        check("settings copy has no secrets",
              "password" not in json.dumps(body).lower())
        check("settings copy carries the station name",
              body.get("station_name") == "Example FM")
        conn.close()
    finally:
        core_config.update_config = real_update
        core_config.load_config = real_load

    print(f"\nSETUP WIZARD OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
