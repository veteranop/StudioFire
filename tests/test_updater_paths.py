"""The updater must back up and restore the database the SERVICES use.

services/updater.py had its own data_dir() = ROOT/data, so on an install whose
data lives in %ProgramData%\\StudioFire (or anywhere pinned) an update would
snapshot the wrong database and a major-version rollback would restore over the
wrong file. These tests pin the app root, point the config at a temp
"ProgramData" and prove backup/restore follow the shared resolver.
"""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import updater  # noqa: E402

passed = 0
failed = []


def check(label, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"ok  : {label}")
    else:
        failed.append(f"{label} {detail}")
        print(f"FAIL: {label} {detail}")


def make_app(paths_block=None, db_dir=None):
    """A fake install: app root + config.json, optional DB in in-app data/."""
    app = tempfile.mkdtemp(prefix="sf-upd-app-")
    os.makedirs(os.path.join(app, "config"), exist_ok=True)
    cfg = {"station_name": "Updater Test"}
    if paths_block is not None:
        cfg["paths"] = paths_block
    with open(os.path.join(app, "config", "config.json"), "w",
              encoding="utf-8") as f:
        json.dump(cfg, f)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(os.path.join(db_dir, "studiofire.db"))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.commit()
        conn.close()
    return app


def read_value(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT x FROM t").fetchone()[0]
    finally:
        conn.close()


def run_with_root(app, fn):
    saved = updater.ROOT
    updater.ROOT = app
    try:
        return fn()
    finally:
        updater.ROOT = saved


def main():
    # ---- 1. data_root pinned at a temp "ProgramData" ---------------------
    machine = tempfile.mkdtemp(prefix="sf-programdata-")
    app = make_app({"data_root": machine})
    data_dir = os.path.join(machine, "data")
    os.makedirs(data_dir, exist_ok=True)
    db = os.path.join(data_dir, "studiofire.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()

    got = run_with_root(app, updater.data_dir)
    check("data_dir follows paths.data_root, not ROOT/data",
          os.path.normpath(got) == os.path.normpath(data_dir), f"-> {got}")
    check("logs_dir follows the same root",
          os.path.normpath(run_with_root(app, updater.logs_dir))
          == os.path.normpath(os.path.join(machine, "logs")))

    bdir = run_with_root(app, lambda: updater.backup("9.9.9"))
    check("backup() writes under the resolved data dir",
          os.path.normpath(os.path.dirname(os.path.dirname(bdir)))
          == os.path.normpath(data_dir), f"-> {bdir}")
    check("backup() snapshots THAT database, with its rows",
          os.path.isfile(os.path.join(bdir, "studiofire.db"))
          and read_value(os.path.join(bdir, "studiofire.db")) == 42)

    # ---- 2. _restore_db writes back to the same database -----------------
    conn = sqlite3.connect(db)
    conn.execute("UPDATE t SET x = 99")
    conn.commit()
    conn.close()
    check("precondition: live DB was changed", read_value(db) == 99)
    run_with_root(app, lambda: updater._restore_db(bdir))
    check("_restore_db puts the snapshot back over the live DB",
          read_value(db) == 42)

    # ---- 3. the reference-station shape: everything pinned --------------
    pinned = {k: tempfile.mkdtemp(prefix=f"sf-pin-{k}-")
              for k in ("data_dir", "logs_dir", "precache_dir", "emergency_dir")}
    app3 = make_app(pinned)
    check("pinned: data_dir is honoured",
          os.path.normpath(run_with_root(app3, updater.data_dir))
          == os.path.normpath(pinned["data_dir"]))
    check("pinned: logs_dir is honoured",
          os.path.normpath(run_with_root(app3, updater.logs_dir))
          == os.path.normpath(pinned["logs_dir"]))

    # all pinned to the app folder itself (the reference station's real shape)
    app4_root = tempfile.mkdtemp(prefix="sf-refstation-")
    inapp = {"data_dir": os.path.join(app4_root, "data"),
             "logs_dir": os.path.join(app4_root, "logs"),
             "precache_dir": os.path.join(app4_root, "precache"),
             "emergency_dir": os.path.join(app4_root, "assets", "emergency")}
    app4 = make_app(inapp, db_dir=inapp["data_dir"])
    check("reference shape: data_dir is the in-app data folder",
          os.path.normpath(run_with_root(app4, updater.data_dir))
          == os.path.normpath(inapp["data_dir"]))
    bdir4 = run_with_root(app4, lambda: updater.backup("9.9.9"))
    check("reference shape: backup still lands in the app's data folder",
          os.path.normpath(bdir4).startswith(
              os.path.normpath(os.path.join(inapp["data_dir"], "updates"))),
          f"-> {bdir4}")
    check("reference shape: backup holds the app DB",
          read_value(os.path.join(bdir4, "studiofire.db")) == 42)

    # ---- 4. legacy in-app DB, config pins nothing ------------------------
    app5 = make_app(None, db_dir=None)
    legacy_data = os.path.join(app5, "data")
    os.makedirs(legacy_data, exist_ok=True)
    conn = sqlite3.connect(os.path.join(legacy_data, "studiofire.db"))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (7)")
    conn.commit()
    conn.close()
    got5 = run_with_root(app5, updater.data_dir)
    check("legacy: an in-app data/studiofire.db is still the one used",
          os.path.normpath(got5) == os.path.normpath(legacy_data), f"-> {got5}")

    print()
    if failed:
        for f in failed:
            print("FAILED:", f)
        print(f"UPDATER PATHS FAILED ({passed} ok, {len(failed)} failed)")
        return 1
    print(f"UPDATER PATHS OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
