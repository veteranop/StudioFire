"""Unit tests for the recurring On-Air schedule (services/core/schedule.py):
daily/weekly slots, run windows (start_date/stop end_date), one-airing-per-day,
the catch-up window, and finish() re-arming recurring vs retiring one-shots.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.core import db as coredb
from services.core import schedule as sched

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("ok  :", name)
    else:
        FAIL += 1
        print("FAIL:", name)


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    coredb.migrate(path)
    conn = coredb.connect(path)
    # a playlist to schedule
    conn.execute("INSERT INTO playlists (name, created_at, updated_at) "
                 "VALUES ('P', 0, 0)")
    conn.commit()
    return conn, path


def at(day: str, hm: str) -> dt.datetime:
    return dt.datetime.strptime(day + " " + hm, "%Y-%m-%d %H:%M")


def main():
    conn, path = fresh_db()
    pid = 1

    # ---- daily slot ----
    sid = sched.add(conn, "playlist", playlist_id=pid, recurrence="daily",
                    time_of_day="06:00")
    # a Wednesday
    check("daily: not due before its time",
          sched.due(conn, at("2026-08-05", "05:59")) is None)
    d = sched.due(conn, at("2026-08-05", "06:00"))
    check("daily: due exactly at its time", d is not None and d["id"] == sid)
    check("daily: due within catch-up window",
          sched.due(conn, at("2026-08-05", "06:15")) is not None)
    check("daily: skipped once past catch-up window",
          sched.due(conn, at("2026-08-05", "06:45")) is None)

    # once fired today, not due again today
    sched.mark_fired(conn, sid, "2026-08-05")
    check("daily: not due again the same day after firing",
          sched.due(conn, at("2026-08-05", "06:05")) is None)
    check("daily: due again the next day",
          sched.due(conn, at("2026-08-06", "06:00")) is not None)

    # ---- weekly: only chosen weekdays ----
    # Mon+Wed+Fri = 1|4|16 = 21. 2026-08-05 is a Wednesday, 08-06 a Thursday.
    wid = sched.add(conn, "playlist", playlist_id=pid, recurrence="weekly",
                    time_of_day="17:00", days_mask=21)
    check("weekly: due on a chosen weekday (Wed)",
          any(e["id"] == wid for e in [sched.due(conn, at("2026-08-05", "17:00"))]
              if e))
    check("weekly: not due on an unchosen weekday (Thu)",
          sched.due(conn, at("2026-08-06", "17:00")) is None
          or sched.due(conn, at("2026-08-06", "17:00"))["id"] != wid)

    # ---- run window: start_date and stop (end_date) ----
    ev = sched.add(conn, "playlist", playlist_id=pid, recurrence="daily",
                   time_of_day="12:00", start_date="2026-08-01",
                   end_date="2026-08-31")
    check("window: not due before start_date",
          _no(sched.due(conn, at("2026-07-31", "12:00")), ev))
    check("window: due inside the window",
          _yes(sched.due(conn, at("2026-08-15", "12:00")), ev))
    check("window: not due after the stop date",
          _no(sched.due(conn, at("2026-09-01", "12:00")), ev))

    # expire_past retires rows whose stop date has passed
    n = sched.expire_past(conn, "2026-09-02")
    check("expire_past retires the ended promo", n >= 1)
    check("expired promo drops off upcoming",
          all(e["id"] != ev for e in sched.list_waiting(conn)))

    # ---- finish(): recurring re-arms, one-shot retires ----
    sched.set_state(conn, sid, "playing")
    sched.finish(conn, sid, "2026-08-10")
    check("finish re-arms a recurring show (waiting)",
          sched.get(conn, sid)["state"] == "waiting")

    once = sched.add(conn, "playlist", playlist_id=pid, recurrence="once",
                     start_at="2026-08-10T09:00")
    sched.set_state(conn, once, "playing")
    sched.finish(conn, once, "2026-08-10")
    check("finish retires a one-time show (done)",
          sched.get(conn, once)["state"] == "done")

    # finish past the stop date retires even a recurring show
    ended = sched.add(conn, "playlist", playlist_id=pid, recurrence="daily",
                      time_of_day="08:00", end_date="2026-08-09")
    sched.set_state(conn, ended, "playing")
    sched.finish(conn, ended, "2026-08-10")
    check("finish retires a recurring show past its stop date",
          sched.get(conn, ended)["state"] == "done")

    # ---- human labels ----
    lbl = sched.get(conn, ev)["when"]
    check("label mentions the stop date", "until 2026-08-31" in lbl)
    wlbl = sched.get(conn, wid)["when"]
    check("weekly label lists the weekdays",
          "Mon" in wlbl and "Wed" in wlbl and "Fri" in wlbl)

    # ---- calendar: occurrences_on resolves recurring + one-shot per date ----
    conn.execute("DELETE FROM playlist_schedule")
    conn.commit()
    daily = sched.add(conn, "playlist", playlist_id=pid, recurrence="daily",
                      time_of_day="06:00", end_date="2026-08-31")
    wk = sched.add(conn, "playlist", playlist_id=pid, recurrence="weekly",
                   time_of_day="17:00", days_mask=21)  # Mon/Wed/Fri
    one = sched.add(conn, "playlist", playlist_id=pid,
                    start_at="2026-08-05T09:00")
    aug5 = sched.occurrences_on(conn, dt.date(2026, 8, 5))   # a Wednesday
    names = {(o["recurrence"], o["time"]) for o in aug5}
    check("calendar: daily show appears on the date", ("daily", "06:00") in names)
    check("calendar: weekly show appears on its weekday (Wed)",
          ("weekly", "17:00") in names)
    check("calendar: one-shot appears on its date", ("once", "09:00") in names)
    check("calendar: sorted by time (06:00 first)", aug5[0]["time"] == "06:00")
    aug6 = sched.occurrences_on(conn, dt.date(2026, 8, 6))   # Thursday
    check("calendar: weekly absent on an off day",
          not any(o["recurrence"] == "weekly" for o in aug6))
    check("calendar: daily absent after its stop date",
          not sched.occurrences_on(conn, dt.date(2026, 9, 1)))

    conn.close()
    try:
        os.remove(path)
    except OSError:
        pass
    print(f"\nSCHEDULE {'OK' if not FAIL else 'FAILED'} "
          f"({PASS} checks, {FAIL} failures)")
    return 1 if FAIL else 0


def _yes(row, sid):
    return row is not None and row["id"] == sid


def _no(row, sid):
    return row is None or row["id"] != sid


if __name__ == "__main__":
    sys.exit(main())
