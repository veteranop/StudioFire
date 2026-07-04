"""Unit tests: spot rule storage + trigger timing (due / next fire).

Run: python tests/test_spots.py
"""
import datetime as dt
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.core import db, spots  # noqa: E402

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def main():
    with tempfile.TemporaryDirectory() as td:
        dbp = os.path.join(td, "core.db")
        db.migrate(dbp)
        conn = db.connect(dbp)

        iid = spots.add(conn, "dir_ads", "interval", interval_min=15)
        cid = spots.add(conn, "dir_station_ids", "clock", clock_minutes="0,30")
        oid = spots.add(conn, "dir_psas", "once", start_at="2020-01-01T00:00")
        mid = spots.add(conn, "dir_jingles", "manual")
        check("four rules stored", len(spots.list_all(conn)) == 4)
        check("label defaulted from folder",
              spots.get(conn, iid)["label"] == "Advertisements / spots")

        now = time.time()
        ri = spots.get(conn, iid)
        check("interval not due immediately", not spots.due(ri, now))
        check("interval due after the interval", spots.due(ri, now + 15 * 60 + 1))
        ne = spots.next_fire_epoch(ri, now)
        check("interval next fire ~15 min out", 14 * 60 < (ne - now) <= 15 * 60 + 2)

        rc = spots.get(conn, cid)
        at_top = dt.datetime(2026, 7, 4, 15, 0, 20).timestamp()
        check("clock due at :00", spots.due(rc, at_top))
        off = dt.datetime(2026, 7, 4, 15, 31, 0).timestamp()
        check("clock not due at :31", not spots.due(rc, off))
        check("clock next fire is in the future",
              spots.next_fire_epoch(rc, off) > off)

        check("once is due (time long past)", spots.due(spots.get(conn, oid), now))
        check("manual never auto-fires", not spots.due(spots.get(conn, mid), now))

        # firing advances the schedule; a 'once' disables itself
        spots.mark_fired(conn, spots.get(conn, oid), now)
        check("once disabled after firing", spots.get(conn, oid)["enabled"] == 0)
        check("once no longer due", not spots.due(spots.get(conn, oid), now))
        spots.mark_fired(conn, spots.get(conn, iid), now)
        check("interval clock reset after firing",
              not spots.due(spots.get(conn, iid), now + 60))

        # clock dedupe: fired this minute -> not due again until next match
        rc = spots.get(conn, cid)
        spots.mark_fired(conn, rc, at_top)
        check("clock not re-due same minute",
              not spots.due(spots.get(conn, cid), at_top + 5))

        check("describe interval", "every 15 min" in spots.describe(
            spots.get(conn, iid)))
        check("describe clock", ":00" in spots.describe(spots.get(conn, cid)))

        spots.set_enabled(conn, iid, False)
        check("disabled rule not in enabled list",
              iid not in {r["id"] for r in spots.list_enabled(conn)})
        spots.remove(conn, mid)
        check("remove deletes the rule", spots.get(conn, mid) is None)
        conn.close()

    print(f"SPOTS OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
