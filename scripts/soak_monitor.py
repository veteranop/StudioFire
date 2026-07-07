"""72-hour soak monitor for the LIVE StudioFire engine (read-only, non-invasive).

Polls the engine status ~1x/s and tails the play journal, recording anything
that would fail the §10.7 gate or regress a known bug:
  - dead air: position frozen > GAP_LIMIT while playing (not paused/emergency)
  - emergency fallbacks (queue drained / NAS gone) — safety net firing
  - mpv restarts (watchdog)
  - phantom content-skips (track_start immediately 'stop'-ended) — the
    every-other-track bug's signature; must stay 0
It appends a rolling summary to logs/soak_report.jsonl every REPORT_EVERY sec.

Pass: longest_gap_s <= 2.0 AND phantom_skips == 0 over the run. Emergency events
are informational (filler is not silence), but a healthy run should have few.

Run detached:  python scripts/soak_monitor.py [hours]   (default 72)
Check anytime:  tail the last line of logs/soak_report.jsonl
"""
import datetime
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS_URL = "http://127.0.0.1:7701/status"
JOURNAL = os.path.join(ROOT, "logs", "play_journal.jsonl")
REPORT = os.path.join(ROOT, "logs", "soak_report.jsonl")
GAP_LIMIT = 2.0
POLL = 1.0
REPORT_EVERY = 300  # 5 min


def get_status():
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def _ts(s):
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


def write_report(rep):
    with open(REPORT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rep) + "\n")


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 72.0
    duration = hours * 3600
    start = time.time()

    last_pos = None
    last_change = start
    stall_counted = False
    longest_gap = 0.0
    gaps_over = 0
    emerg = restarts = phantoms = tracks = 0
    engine_down_s = 0.0
    joff = os.path.getsize(JOURNAL) if os.path.exists(JOURNAL) else 0
    prev = None  # (event, ts) for phantom detection
    last_report = start
    now_title = None

    write_report({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                  "event": "soak_started", "hours": hours})

    while time.time() - start < duration:
        t = time.time()
        s = get_status()
        if s is None:
            engine_down_s += POLL
            last_pos, last_change, stall_counted = None, t, False
        else:
            now_title = s.get("now_title")
            playing = (s.get("now_playing") and not s.get("paused")
                       and not s.get("emergency_mode"))
            pos = s.get("position")
            if playing and isinstance(pos, (int, float)):
                if last_pos is None or abs(pos - last_pos) > 0.01:
                    last_pos, last_change, stall_counted = pos, t, False
                else:
                    gap = t - last_change
                    longest_gap = max(longest_gap, gap)
                    if gap > GAP_LIMIT and not stall_counted:
                        gaps_over += 1
                        stall_counted = True
            else:
                last_pos, last_change, stall_counted = None, t, False

        # tail the journal (handle rotation: size shrank -> start over)
        try:
            sz = os.path.getsize(JOURNAL)
            if sz < joff:
                joff = 0
            if sz > joff:
                with open(JOURNAL, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(joff)
                    chunk = f.read()
                    joff = f.tell()
                for line in chunk.splitlines():
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    ev = e.get("event")
                    if ev == "emergency_enter":
                        emerg += 1
                    elif ev == "mpv_restart":
                        restarts += 1
                    elif ev == "track_start":
                        tracks += 1
                    if (prev and prev[0] == "track_start" and ev == "track_end"
                            and e.get("reason") == "stop"):
                        a, b = _ts(prev[1]), _ts(e.get("ts"))
                        if a is not None and b is not None and (b - a) < 0.3:
                            phantoms += 1
                    prev = (ev, e.get("ts"))
        except OSError:
            pass

        if t - last_report >= REPORT_EVERY:
            write_report({
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "elapsed_h": round((t - start) / 3600, 2),
                "longest_gap_s": round(longest_gap, 2),
                "dead_air_over_2s": gaps_over,
                "emergency_events": emerg,
                "mpv_restarts": restarts,
                "phantom_skips": phantoms,
                "tracks_aired": tracks,
                "engine_unreachable_s": round(engine_down_s, 1),
                "now": now_title})
            last_report = t
        time.sleep(POLL)

    write_report({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                  "event": "soak_complete", "elapsed_h": round(hours, 2),
                  "longest_gap_s": round(longest_gap, 2),
                  "dead_air_over_2s": gaps_over, "phantom_skips": phantoms,
                  "emergency_events": emerg, "mpv_restarts": restarts,
                  "tracks_aired": tracks,
                  "PASS": longest_gap <= GAP_LIMIT and phantoms == 0})
    return 0


if __name__ == "__main__":
    sys.exit(main())
