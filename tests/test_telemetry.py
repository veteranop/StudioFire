"""Feature-usage telemetry is OPT-IN: default off, random install ID, no station name.

What this file locks down (the claim the website makes):

* a config with no telemetry key sends nothing — no tag in the page at all,
* opting in renders the tag with the random install ID and NOT the station name,
* an existing install (config written before the setting existed) stays off,
* the install ID is minted once and survives turning the switch off and on,
* the Settings endpoint flips it live, for an admin only.

Run: python tests/test_telemetry.py
"""
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient                  # noqa: E402

from services.core import auth, config as core_config, db   # noqa: E402
from services.core.app import create_app                    # noqa: E402

STATION = "KDPI Test Station 88.5"
passed = 0
GA_TAG = "googletagmanager.com"


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def write_config(td, core=None, paths=True):
    """A config on disk, exactly as an install would have it.

    ``paths`` is what keeps the throwaway app inside ``td`` — without it the
    loader falls back to the machine's real data folder and a test would write
    users into the developer's own database.
    """
    cfgp = os.path.join(td, "config.json")
    body = {"station_name": STATION, "core": core or {}}
    if paths:
        body["paths"] = {"data_dir": os.path.join(td, "data"),
                         "logs_dir": os.path.join(td, "logs"),
                         "precache_dir": os.path.join(td, "precache"),
                         "emergency_dir": os.path.join(td, "emergency"),
                         "state_path": os.path.join(td, "state.json"),
                         "heartbeat_path": os.path.join(td, "hb.txt")}
    with open(cfgp, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
    return cfgp


def render(cfgp, path="/setup"):
    """Load the config the way the service does, build the app, render a page."""
    os.environ["STUDIOFIRE_CONFIG"] = cfgp
    cfg = core_config.load_config()
    app = create_app(cfg)
    client = TestClient(app, follow_redirects=False)
    return cfg, client, client.get(path)


def ga_block(html):
    """The GA4 script tags only — the part that could send something."""
    start = html.find("<!-- GA4")
    if start == -1:
        return ""
    end = html.find("</script>", html.find("gtag('config'", start))
    return html[start:end]


def main():
    # -- 1. a default config sends nothing ----------------------------------
    td = tempfile.mkdtemp(prefix="sf-tel-default-")
    cfgp = write_config(td, core={})
    cfg, client, page = render(cfgp)
    check("sandbox: the app's database is inside the temp dir",
          os.path.abspath(cfg["db_path"]).startswith(os.path.abspath(td)))
    check("default config: telemetry_enabled is False", cfg["telemetry_enabled"] is False)
    check("default config: no GA measurement id", cfg["ga_measurement_id"] == "")
    check("default config: no install id", cfg["telemetry_install_id"] == "")
    check("default config: page renders", page.status_code == 200)
    check("default config: no tracking tag in the page", GA_TAG not in page.text)

    # -- 2. opt in: tag + install ID, never the station name ----------------
    t = core_config.set_telemetry(True, cfgp)
    check("opt-in: enabled", t["enabled"] is True)
    check("opt-in: install id minted", re.fullmatch(r"[0-9a-f]{32}", t["install_id"]))
    cfg, client, page = render(cfgp)
    block = ga_block(page.text)
    check("opt-in: the GA4 tag is now in the page", GA_TAG in page.text)
    check("opt-in: payload carries the install ID", t["install_id"] in block)
    check("opt-in: install ID is the ONLY identifier in the payload",
          "user_properties" in block and "install_id" in block)
    check("opt-in: station name is NOT in the payload", STATION not in block)
    check("opt-in: the page still renders the station name normally",
          STATION in page.text)
    check("opt-in: no station-name user property left behind",
          "user_properties', {station" not in page.text
          and '"station"' not in block)

    # -- 3. turning it off stops it again ----------------------------------
    off = core_config.set_telemetry(False, cfgp)
    check("off again: enabled is False", off["enabled"] is False)
    check("off again: the install ID is kept", off["install_id"] == t["install_id"])
    cfg, client, page = render(cfgp)
    check("off again: no tracking tag in the page", GA_TAG not in page.text)

    # -- 4. existing install (no key) stays off, and is not opted in --------
    td2 = tempfile.mkdtemp(prefix="sf-tel-existing-")
    cfgp2 = write_config(td2, core={"bind_host": "0.0.0.0", "bind_port": 8080})
    os.environ["STUDIOFIRE_CONFIG"] = cfgp2
    legacy = core_config.load_config()
    check("existing config: telemetry reads as OFF",
          legacy["telemetry_enabled"] is False
          and legacy["ga_measurement_id"] == ""
          and legacy["telemetry_install_id"] == "")
    cfg, client, page = render(cfgp2)
    check("existing config: no tracking tag in the page", GA_TAG not in page.text)

    # -- 5. the settings switch, end to end (admin only) -------------------
    cfg = core_config.load_config()
    conn = db.connect(cfg["db_path"])
    auth.create_user(conn, "admin", "pw123456", "admin")
    conn.close()
    login = client.post("/login",
                        data={"username": "admin", "password": "pw123456"},
                        follow_redirects=False)
    check("settings: admin can sign in", login.status_code in (200, 303))
    got = client.get("/api/settings/telemetry")
    check("settings: GET reports off before anyone opts in",
          got.status_code == 200 and got.json()["enabled"] is False)
    on = client.post("/api/settings/telemetry", json={"enabled": True})
    check("settings: POST turns it on", on.status_code == 200 and on.json()["ok"])
    new_id = on.json()["install_id"]
    check("settings: turning it on minted an install ID",
          re.fullmatch(r"[0-9a-f]{32}", new_id or ""))
    page = client.get("/settings")
    check("settings: the next page render carries the tag",
          GA_TAG in page.text and new_id in page.text)
    check("settings: still no station name in the payload",
          STATION not in ga_block(page.text))
    back = client.post("/api/settings/telemetry", json={"enabled": False})
    check("settings: POST turns it off again",
          back.status_code == 200 and back.json()["enabled"] is False)
    check("settings: the tag disappears on the next render",
          GA_TAG not in client.get("/settings").text)

    # the switch is a statement about the station, so operators cannot flip it
    conn = db.connect(cfg["db_path"])
    auth.create_user(conn, "op", "pw123456", "operator")
    conn.close()
    op_client = TestClient(create_app(core_config.load_config()),
                           follow_redirects=False)
    op_client.post("/login", data={"username": "op", "password": "pw123456"},
                   follow_redirects=False)
    denied = op_client.post("/api/settings/telemetry", json={"enabled": True})
    check("settings: an operator cannot change it", denied.status_code == 403)

    print(f"\nTELEMETRY OPT-IN OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
