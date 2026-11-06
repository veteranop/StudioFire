"""Unit tests: the single honest station-health verdict (release A, item 2).

The verdict is the one thing the header pill reads, and the rule is absolute:
it must never read "ok" while audio, the engine, or the spot policy is wrong.

Run: python tests/test_status_verdict.py
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db, spots, status  # noqa: E402

passed = 0


def check(name, cond, extra=""):
    global passed
    if not cond:
        print("FAIL:", name, extra)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


LIVE = lambda: {"paused": False, "emergency_mode": False, "mpv_alive": True}  # noqa: E731
LOUD = lambda: {"enabled": True, "channels": [{"peak": -12.0}, {"peak": -13.0}]}  # noqa: E731
SILENT = lambda: {"enabled": True, "channels": [{"peak": -70.0}, {"peak": -70.0}]}  # noqa: E731


def main():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        dbp = os.path.join(td, "core.db")
        db.migrate(dbp)
        conn = db.connect(dbp)
        music = os.path.join(td, "music")
        os.makedirs(music, exist_ok=True)
        cfg = {"data_dir": td, "nas_music_root": music, "path_aliases": {}}

        def heartbeat(age_sec=0.0):
            with open(os.path.join(td, "engine_heartbeat.txt"), "w") as f:
                f.write(str(time.time() - age_sec))

        def reset():
            db.set_setting(conn, status._SILENCE_KEY, "")

        now = time.time()
        heartbeat()

        # ---- the good case ----
        ads = spots.add(conn, "dir_ads", "interval", interval_min=15)
        ids = spots.add(conn, "dir_station_ids", "clock", clock_minutes="0")
        spots.add(conn, "dir_psas", "manual")
        v = status.verdict(conn, cfg, LIVE(), LOUD(), now)
        check("all healthy => ok", v["state"] == "ok", v["headline"])
        check("ok carries the checked facts",
              v["checked"]["enabled_spot_rules"] == 3
              and v["checked"]["legal_id_rules"] == 1)

        # ---- engine gone ----
        v = status.verdict(conn, cfg, None, None, now)
        check("engine unreachable => fault", v["state"] == "fault", v["headline"])
        check("fault names the engine", "engine" in v["headline"].lower(),
              v["headline"])

        # ---- paused / emergency ----
        v = status.verdict(conn, cfg, {"paused": True, "emergency_mode": False},
                           LOUD(), now)
        check("paused while claiming on-air => fault", v["state"] == "fault")
        v = status.verdict(conn, cfg, {"paused": False, "emergency_mode": True},
                           LOUD(), now)
        check("emergency filler => fault", v["state"] == "fault")
        check("fault names emergency", "emergency" in v["headline"].lower(),
              v["headline"])

        # ---- stale heartbeat ----
        heartbeat(age_sec=120)
        v = status.verdict(conn, cfg, LIVE(), LOUD(), now)
        check("stale heartbeat => fault", v["state"] == "fault", v["headline"])
        heartbeat()

        # ---- spot policy empty (the 2026-11-02 failure) ----
        for rid in [r["id"] for r in spots.list_all(conn)]:
            spots.remove(conn, rid)
        v = status.verdict(conn, cfg, LIVE(), LOUD(), now)
        check("zero spot rules => fault", v["state"] == "fault", v["headline"])
        blob = " ".join(r["text"] for r in v["reasons"]).lower()
        check("fault explains nothing airs between songs", "spot" in blob, blob)

        # ---- rules back, but no legal ID ----
        for rid in [r["id"] for r in spots.list_deleted(conn)]:
            spots.restore(conn, rid)
        spots.remove(conn, ids)
        v = status.verdict(conn, cfg, LIVE(), LOUD(), now)
        check("no legal-ID rule => fault", v["state"] == "fault", v["headline"])
        blob = " ".join(r["text"] for r in v["reasons"]).lower()
        check("fault names the legal ID", "legal" in blob, blob)
        spots.restore(conn, ids)

        # ---- silence latches: warn at 15s, fault past 60s ----
        reset()
        status.verdict(conn, cfg, LIVE(), SILENT(), now)
        v = status.verdict(conn, cfg, LIVE(), SILENT(), now + 20)
        check("20s of silence => warn", v["state"] == "warn", v["headline"])
        v = status.verdict(conn, cfg, LIVE(), SILENT(), now + 65)
        check("65s of silence => fault", v["state"] == "fault", v["headline"])
        check("silence duration is reported", v["silence_sec"] > 60,
              str(v["silence_sec"]))
        # (simulated clock moved 70s ahead, so re-stamp the heartbeat as a real
        #  engine would; otherwise the stale-heartbeat fault is the correct answer)
        heartbeat()
        now2 = time.time()
        v = status.verdict(conn, cfg, LIVE(), LOUD(), now2)
        check("audio back => silence clock resets", v["silence_sec"] == 0.0)
        check("audio back => ok again", v["state"] == "ok", v["headline"])

        # ---- library unreachable is a warning, never a fault ----
        bad = dict(cfg, nas_music_root=os.path.join(td, "nope"))
        v = status.verdict(conn, bad, LIVE(), LOUD(), now2)
        check("NAS unreachable => warn", v["state"] == "warn", v["headline"])
        check("warn names the library", "library" in v["headline"].lower()
              or "nas" in v["headline"].lower(), v["headline"])

        # ---- recent missing-file skips surface as a warning ----
        conn.execute("INSERT INTO skipped_items(ts, path, reason, playlist_id) "
                     "VALUES (?, ?, ?, ?)",
                     (now - 60, "Z:\\gone.mp3", "missing", None))
        conn.commit()
        v = status.verdict(conn, cfg, LIVE(), LOUD(), now)
        check("recent skips => warn", v["state"] == "warn", v["headline"])
        check("warn counts the missing songs", "missing" in v["headline"].lower(),
              v["headline"])
        check("skip count is in checked",
              v["checked"]["skipped_last_15min"] >= 1)

        # ---- equipment pings are deliberately NOT part of the verdict ----
        v = status.verdict(conn, cfg, LIVE(), LOUD(), now2)
        check("verdict does not depend on equipment pings",
              "equipment" not in " ".join(r["text"] for r in v["reasons"]).lower())

    print("\nSTATUS VERDICT OK (%d checks)" % passed)


if __name__ == "__main__":
    main()
