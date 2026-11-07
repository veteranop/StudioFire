"""First-run setup: the steps, their validation, and where each answer goes.

A station manager with no IT background must get from "installer finished" to
"on air" without editing JSON or mapping a drive. This module owns that flow:
one step per screen, every answer written where the rest of the app already
reads it, and every step revisitable later from Settings.

Design notes
------------
* **No config file is required to start.** Absence of config.json is a normal
  first-run state, not an error.
* **Nothing here touches the playout path.** Steps write settings/config and, at
  most, ask the indexer to scan.
* **Validation returns a sentence, not a code.** Every error is written as what
  the operator should do next ("StudioFire can't see that folder. If it's on
  another computer, use its network path, like \\\\NAS\\Music.").
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

from . import auth, db

# Order matters: this is the wizard.
STEPS = [
    ("station", "Your station"),
    ("admin", "Administrator account"),
    ("library", "Music library"),
    ("playlists", "Playlists"),
    ("spots", "Station IDs and ads"),
    ("audio", "Audio output"),
    ("emergency", "If the music stops"),
    ("streaming", "Streaming (optional)"),
    ("done", "Ready to go"),
]

KEY_STEP = "setup.step"
KEY_DONE = "setup.completed"

DEFAULTS_HELP = {
    "station": "The name listeners know, and how you like to see the time.",
    "admin": "The account you sign in with. There is no default password.",
    "library": "Where your music lives — a folder on this PC or a network share.",
    "playlists": "Where playlist files (.lst) are kept. You can import your old ones.",
    "spots": "Optional. The folders holding IDs, PSAs, jingles and paid spots.",
    "audio": "The output device StudioFire should use. The default is usually right.",
    "emergency": "What plays if the music runs out — StudioFire fills this itself.",
    "streaming": "Optional. Song titles on your website player and the stream host.",
    "done": "Everything can be changed later in Settings.",
}


# --------------------------------------------------------------- state
def current_step(conn) -> str:
    """The first step that is not finished yet."""
    if (db.get_setting(conn, KEY_DONE) or "") == "1":
        return "done"
    step = db.get_setting(conn, KEY_STEP) or STEPS[0][0]
    ids = [s for s, _ in STEPS]
    return step if step in ids else STEPS[0][0]


def set_step(conn, step: str) -> None:
    db.set_setting(conn, KEY_STEP, step)


def complete(conn) -> None:
    db.set_setting(conn, KEY_DONE, "1")


def is_complete(conn) -> bool:
    return (db.get_setting(conn, KEY_DONE) or "") == "1"


def state(conn, cfg: dict | None = None) -> dict:
    """What the wizard needs to render itself."""
    step = current_step(conn)
    ids = [s for s, _ in STEPS]
    cfg = cfg or {}
    answers = {
        "station_name": cfg.get("station_name") or "",
        "time_format": db.get_setting(conn, "time_format") or "12",
        "music_root": cfg.get("nas_music_root") or "",
        "playlists_dir": db.get_setting(conn, "playlist_lst_dir") or "",
        "audio_device_guid": (cfg.get("audio_device_guid") or ""),
        "np_file": db.get_setting(conn, "np_file") or "",
        "stream_endpoint": db.get_setting(conn, "stream_endpoint") or "",
    }
    for key, _lbl in (("dir_station_ids", None), ("dir_psas", None),
                      ("dir_jingles", None), ("dir_ads", None),
                      ("dir_shows", None)):
        answers[key] = db.get_setting(conn, key) or ""
    return {
        "step": step,
        "index": ids.index(step) if step in ids else 0,
        "steps": [{"id": s, "label": lbl, "help": DEFAULTS_HELP.get(s, "")}
                  for s, lbl in STEPS],
        "answers": answers,
        "complete": is_complete(conn),
        "has_users": auth.any_users(conn),
        "default_playlists_dir": default_playlists_dir(cfg),
    }


def default_playlists_dir(cfg: dict | None = None) -> str:
    base = (cfg or {}).get("data_dir") or os.path.join(
        os.environ.get("ProgramData", os.path.expanduser("~")), "StudioFire")
    return os.path.join(base.replace("\\data", ""), "Playlists")


# --------------------------------------------------------------- validation
def check_folder(path: str) -> str | None:
    """None when readable, otherwise a sentence telling them what to do."""
    if not path:
        return "Choose a folder first."
    p = (path or "").strip().strip('"')
    if not os.path.exists(p):
        if p.upper().startswith("\\\\") or p.startswith("//"):
            return ("StudioFire can't see that network folder. Check the share "
                    "name is exactly right (for example \\\\NAS\\Music) and that "
                    "this PC can open it in File Explorer first.")
        return ("StudioFire can't see that folder. If it's on another computer, "
                "use its network path, like \\\\NAS\\Music.")
    if not os.path.isdir(p):
        return "That is a file, not a folder. Pick the folder that holds the files."
    try:
        os.listdir(p)
    except OSError as exc:
        return ("The folder is there but StudioFire can't read it (%s). Check "
                "the share permissions, or pick a folder you can open." % exc)
    return None


def count_audio_files(path: str, limit: int = 20000) -> int:
    """A quick count so the wizard can say 'Found N songs'."""
    exts = (".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma")
    n = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            if f.lower().endswith(exts):
                n += 1
                if n >= limit:
                    return n
    return n


def audio_devices() -> list[dict]:
    """What mpv can see, so the operator can pick by name."""
    mpv = shutil.which("mpv") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "bin", "mpv.exe")
    out: list[dict] = []
    if not os.path.isfile(mpv):
        return out
    try:
        res = subprocess.run([mpv, "--audio-device=help"], capture_output=True,
                             text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return out
    for line in (res.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("'") or "=" not in line:
            continue
        # 'wasapi/{guid}' (Windows WASAPI)  --  Speakers (Realtek)
        try:
            ident, desc = line.split("=", 1)
        except ValueError:
            continue
        ident = ident.strip().strip("'").strip()
        desc = desc.strip()
        if not ident.startswith("wasapi/"):
            continue
        out.append({"id": ident, "label": desc,
                    "guid": ident.split("/", 1)[1]})
    return out


def test_tone(guid: str = "", seconds: float = 1.5) -> tuple[bool, str]:
    """Play a short tone so a human can confirm the output.

    Honest limit: when StudioFire runs as a Windows service it lives in session
    0, which has no audio endpoint. A tone played from the web UI is proof that
    mpv and the device exist — the proof that sound reaches the transmitter is
    the On Air page and your ears at the receiver.
    """
    mpv = shutil.which("mpv") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "bin", "mpv.exe")
    if not os.path.isfile(mpv):
        return False, "mpv is missing, so no test tone can play."
    cmd = [mpv, "--no-video", "--really-quiet", "--volume=40",
           "--length=%.1f" % seconds]
    if guid:
        cmd.append("--audio-device=wasapi/" + guid)
    cmd.append("av://lavfi:sine=frequency=440:sample_rate=44100")
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as exc:
        return False, "Could not start the tone: %s" % exc
    return True, "Playing a 1.5-second tone at 440 Hz."
