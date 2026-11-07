"""P1 (engine) and P2 (core) must resolve every path identically.

This is the blocker from the PR review: when the two services disagreed, the
engine wrote the play journal and heartbeat where the core never looked (empty
reports, and a hung engine that never faulted), and its emergency/precache
fallbacks read folders the core never filled.

Run:  python tests/test_path_parity.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.core import config as core_config          # noqa: E402
from services.engine import main as engine_main          # noqa: E402

passed = 0
failed = []


def check(label, cond, detail=""):
    global passed
    if cond:
        passed += 1
        print(f"ok  : {label}")
    else:
        failed.append(f"{label} {detail}".strip())
        print(f"FAIL: {label} {detail}")


KEYS = ("data_root", "data_dir", "logs_dir", "precache_dir", "emergency_dir",
        "state_path", "journal_path", "heartbeat_path")


def write_cfg(tmp, paths):
    cfg = {"station_name": "Parity Test", "paths": paths}
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


def both(tmp, cfg_path, app_root=None):
    """Resolve the same config through both services.

`app_root` is pinned for the duration because both modules default to the repo
folder — in this checkout that contains a real data/studiofire.db, which would
otherwise trip the legacy-layout rule on every case.
"""
    saved = (core_config.ROOT, engine_main.ROOT)
    core_config.ROOT = app_root or saved[0]
    engine_main.ROOT = app_root or saved[1]
    try:
        core = core_config.load_config(cfg_path)
        engine = engine_main.load_config(cfg_path)
    finally:
        core_config.ROOT, engine_main.ROOT = saved
    return core, engine


def compare(label, core, engine):
    diffs = {k: (core.get(k), engine.get(k)) for k in KEYS
             if core.get(k) != engine.get(k)}
    check(label, not diffs, f"-> {diffs}")


def run_case(label, paths, expect_root=None, app_root=None):
    tmp = tempfile.mkdtemp(prefix="sf-parity-")
    cfg_path = write_cfg(tmp, paths)
    if app_root is None:
        app_root = tempfile.mkdtemp(prefix="sf-app-")   # a fresh install
    core, engine = both(tmp, cfg_path, app_root)
    compare(label, core, engine)
    if expect_root is not None:
        check(f"{label}: data root is {os.path.basename(expect_root) or expect_root}",
              os.path.normpath(core["data_root"]) == os.path.normpath(expect_root),
              f"-> {core['data_root']}")
    for key in ("state_path", "journal_path", "heartbeat_path"):
        check(f"{label}: {key} sits under its folder",
              os.path.dirname(core[key]) in (os.path.normpath(core["data_dir"]),
                                             os.path.normpath(core["logs_dir"])),
              f"-> {core[key]}")
    return core, engine


def main():
    # A fake machine-wide data folder, so the default branch is provable.
    programdata = tempfile.mkdtemp(prefix="sf-programdata-")
    os.environ["ProgramData"] = programdata
    expected_default = os.path.join(programdata, "StudioFire")

    real_name = os.name
    try:
        # --- 1. no paths at all: the fresh-install shape ---------------------
        core, engine = run_case("no paths at all", {}, expect_root=expected_default)
        check("no paths: data_dir is under the machine-wide root",
              core["data_dir"] == os.path.join(expected_default, "data"),
              f"-> {core['data_dir']}")
        check("no paths: logs, precache and emergency are siblings",
              core["logs_dir"] == os.path.join(expected_default, "logs")
              and core["precache_dir"] == os.path.join(expected_default, "precache")
              and core["emergency_dir"] == os.path.join(expected_default, "emergency"),
              f"-> {core['logs_dir']} / {core['precache_dir']} / {core['emergency_dir']}")

        # --- 2. data_root only: everything under it --------------------------
        root = tempfile.mkdtemp(prefix="sf-dataroot-")
        core, _engine = run_case("data_root only", {"data_root": root},
                                 expect_root=root)
        check("data_root only: subfolders derived from it",
              core["data_dir"] == os.path.join(root, "data")
              and core["logs_dir"] == os.path.join(root, "logs")
              and core["precache_dir"] == os.path.join(root, "precache")
              and core["emergency_dir"] == os.path.join(root, "emergency"),
              f"-> {core['data_dir']}")

        # --- 3. every path pinned: the reference station's shape -------------
        pinned = {k: tempfile.mkdtemp(prefix=f"sf-pin-{k}-")
                  for k in ("data_dir", "logs_dir", "precache_dir", "emergency_dir")}
        core, engine = run_case("every path pinned", pinned)
        for key, want in pinned.items():
            check(f"pinned {key} is honoured by both",
                  os.path.normpath(core[key]) == os.path.normpath(want)
                  and os.path.normpath(engine[key]) == os.path.normpath(want),
                  f"-> {core[key]} / {engine[key]}")
        check("pinned paths: journal follows logs_dir, heartbeat follows data_dir",
              core["journal_path"] == os.path.join(pinned["logs_dir"],
                                                   "play_journal.jsonl")
              and core["heartbeat_path"] == os.path.join(pinned["data_dir"],
                                                         "engine_heartbeat.txt"),
              f"-> {core['journal_path']} / {core['heartbeat_path']}")

        # --- 4. a half-pinned config (data_dir but no logs_dir) --------------
        half = {"data_dir": pinned["data_dir"]}
        core, engine = run_case("data_dir pinned, logs defaulted", half)
        check("half-pinned: logs fall back to the machine-wide root",
              os.path.normpath(core["logs_dir"]) ==
              os.path.normpath(os.path.join(expected_default, "logs")),
              f"-> {core['logs_dir']}")

        # --- 5. os.name = 'nt' with ProgramData set --------------------------
        os.name = "nt"
        core, engine = run_case("os.name='nt' with ProgramData",
                                {}, expect_root=expected_default)
        os.name = real_name

        # --- 6. no ProgramData and not Windows: beside the app --------------
        os.environ.pop("ProgramData", None)
        os.name = "posix"
        posix_app = tempfile.mkdtemp(prefix="sf-posix-")
        core, engine = run_case("no ProgramData (POSIX): beside the app", {},
                                app_root=posix_app)
        check("POSIX: data root is the app folder",
              os.path.normpath(core["data_root"]) ==
              os.path.normpath(posix_app),
              f"-> {core['data_root']}")

        # --- 6b. legacy layout: a DB beside the app, config pins nothing ----
        legacy_app = tempfile.mkdtemp(prefix="sf-legacy-")
        os.makedirs(os.path.join(legacy_app, "data"))
        open(os.path.join(legacy_app, "data", "studiofire.db"), "wb").close()
        core, engine = run_case("legacy data/ beside the app, nothing pinned",
                                {}, expect_root=legacy_app, app_root=legacy_app)
        check("legacy: the existing database is the one used",
              os.path.normpath(core["data_dir"]) ==
              os.path.normpath(os.path.join(legacy_app, "data")),
              f"-> {core['data_dir']}")
    finally:
        os.name = real_name
        os.environ["ProgramData"] = programdata

    # --- 7. the emergency folder is seeded, once ---------------------------
    from services import paths as shared_paths
    root = tempfile.mkdtemp(prefix="sf-emerg-")
    app = tempfile.mkdtemp(prefix="sf-app-")
    os.makedirs(os.path.join(app, "assets", "emergency"))
    with open(os.path.join(app, "assets", "emergency", "clip.mp3"), "wb") as f:
        f.write(b"ID3fake")
    resolved = shared_paths.resolve({"data_root": root}, app)
    first = shared_paths.seed_emergency(resolved, app)
    second = shared_paths.seed_emergency(resolved, app)
    check("emergency clips are copied on first run, not again",
          first == 1 and second == 0
          and os.path.isfile(os.path.join(resolved["emergency_dir"], "clip.mp3")),
          f"-> {first}/{second}")
    check("seeded emergency lands where the engine reads",
          os.path.normpath(resolved["emergency_dir"]) ==
          os.path.normpath(os.path.join(root, "emergency")),
          f"-> {resolved['emergency_dir']}")

    print()
    if failed:
        for f in failed:
            print("FAILED:", f)
        print(f"PATH PARITY FAILED ({passed} ok, {len(failed)} failed)")
        return 1
    print(f"PATH PARITY OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
