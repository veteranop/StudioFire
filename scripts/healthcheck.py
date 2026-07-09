r"""One-shot health probe for the StudioFire stack (read-only, safe to run live).

Answers the question we couldn't answer during the move: "the web is up, but am I
actually ON AIR, or silently stuck looping emergency filler?" Checks:
  - P2 core    : GET http://127.0.0.1:<core_port>/health   (is the web/API alive)
  - P1 engine  : GET http://127.0.0.1:<engine_port>/status (is it playing real audio)

Ports come from config\config.json (falls back to 8080 / 7701). Prints a concise
report and exits non-zero if anything is wrong, so it can gate a scheduled task:
  0  healthy and on air
  1  a problem worth looking at (see the printed lines)

Run:  python scripts/healthcheck.py        (or just double-click healthcheck.bat)
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_json(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.load(r)
    except Exception:
        return None


def _ports():
    """Read the bound ports from config; defaults match config.example."""
    core, engine = 8080, 7701
    try:
        with open(os.path.join(ROOT, "config", "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        core = int(cfg.get("core", {}).get("bind_port", core))
        engine = int(cfg.get("engine", {}).get("ipc_port", engine))
    except Exception:
        pass
    return core, engine


def main():
    core_port, engine_port = _ports()
    problems = []

    # P2 core -------------------------------------------------------------
    core = _get_json(f"http://127.0.0.1:{core_port}/health")
    if core and core.get("ok"):
        print(f"[ OK ] P2 core   : up on :{core_port}")
    else:
        print(f"[FAIL] P2 core   : no /health response on :{core_port} (is it running?)")
        problems.append("core down")

    # P1 engine -----------------------------------------------------------
    st = _get_json(f"http://127.0.0.1:{engine_port}/status")
    if st is None:
        print(f"[FAIL] P1 engine : unreachable on :{engine_port} (is it running?)")
        problems.append("engine unreachable")
    else:
        forced = st.get("forced_emergency")
        emergency = st.get("emergency_mode")
        title = st.get("now_title") or st.get("now_playing") or "(nothing)"
        source = st.get("now_source", "?")
        qlen = st.get("queue_len")

        if not st.get("mpv_alive", True):
            print("[FAIL] P1 engine : mpv is NOT alive (no audio output)")
            problems.append("mpv dead")

        if emergency and not forced:
            # The exact symptom from the move: alive but looping filler because
            # P2 stopped feeding the queue. On air only in the safety-net sense.
            print(f"[FAIL] P1 engine : EMERGENCY MODE - looping filler, not your library")
            print(f"                   now: {title}  (queue_len={qlen})")
            problems.append("emergency mode (queue not being fed)")
        elif forced:
            print(f"[WARN] P1 engine : forced emergency (operator-set) - filler by choice")
            print(f"                   now: {title}")
        else:
            paused = " [PAUSED]" if st.get("paused") else ""
            print(f"[ OK ] P1 engine : on air{paused}  source={source}  queue_len={qlen}")
            print(f"                   now: {title}")

    print()
    if problems:
        print("RESULT: NOT healthy -> " + "; ".join(problems))
        return 1
    print("RESULT: healthy and on air")
    return 0


if __name__ == "__main__":
    sys.exit(main())
