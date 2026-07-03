"""Phase 0 smoke test: mpv JSON IPC over a Windows named pipe.

Verifies the exact mechanism P1 depends on: launch bin/mpv.exe idle,
connect to its IPC pipe, issue commands, read responses, enumerate
audio output devices. No audio files required.

Run: python tests/ipc_smoke.py
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MPV = os.path.join(ROOT, "bin", "mpv.exe")
PIPE_NAME = "studiofire-ipc-smoke"
PIPE_PATH = "\\\\.\\pipe\\" + PIPE_NAME


def main() -> int:
    proc = subprocess.Popen(
        [MPV, "--no-config", "--idle=yes", "--no-video", "--no-terminal",
         "--input-ipc-server=" + PIPE_PATH],
    )
    f = None
    try:
        for _ in range(50):
            if proc.poll() is not None:
                print("FAIL: mpv exited early with code", proc.returncode)
                return 1
            try:
                f = open(PIPE_PATH, "r+b", buffering=0)
                break
            except OSError:
                time.sleep(0.1)
        if f is None:
            print("FAIL: IPC pipe never appeared at", PIPE_PATH)
            return 1

        def cmd(*args):
            f.write((json.dumps({"command": list(args)}) + "\n").encode())
            # skip async events; return the first response with an error field
            while True:
                resp = json.loads(f.readline())
                if "error" in resp:
                    return resp

        print("mpv-version :", cmd("get_property", "mpv-version")["data"])
        print("idle-active :", cmd("get_property", "idle-active")["data"])
        devices = cmd("get_property", "audio-device-list")["data"]
        print("audio devices (%d):" % len(devices))
        for d in devices:
            print("   ", d["name"], "|", d.get("description", ""))
        print("IPC OK")
        return 0
    finally:
        if f is not None:
            f.close()
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
