"""Orphaned-mpv regression test (seen live 2026-09-25).

An engine stopped with Ctrl+C left its mpv running; the next engine then
connected to that orphan's pipe and drove the wrong player. At startup the
engine must kill any mpv still serving ITS pipe — and nothing else.

Real mpv, null audio. Run: python -m tests.test_stale_mpv
"""
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.engine.supervisor import EngineSupervisor           # noqa: E402
from tests.test_supervisor_bench import (MPV, build_config,       # noqa: E402
                                         make_wav, wait_for)

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def spawn_mpv(pipe_name):
    return subprocess.Popen(
        [MPV, "--no-config", "--no-video", "--no-terminal", "--idle=yes",
         "--ao=null", "--input-ipc-server=\\\\.\\pipe\\" + pipe_name],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main():
    td = tempfile.mkdtemp(prefix="sf-stale-")
    song = os.path.join(td, "song.wav")
    make_wav(song, seconds=3.0)
    emdir = os.path.join(td, "emergency")
    os.makedirs(emdir)
    cfg = build_config(td, emdir)
    cfg["pipe_name"] += "-stale"

    orphan = spawn_mpv(cfg["pipe_name"])           # a dead engine's leftover
    bystander = spawn_mpv(cfg["pipe_name"] + "-other")  # someone else's
    time.sleep(1.0)
    check("setup: orphan + bystander mpv running",
          orphan.poll() is None and bystander.poll() is None)

    sup = EngineSupervisor(cfg)
    sup.start()
    try:
        check("engine startup killed the orphan on its pipe",
              wait_for(lambda: orphan.poll() is not None, 10, "orphan dies"))
        check("an mpv on a DIFFERENT pipe is left alone",
              bystander.poll() is None)
        check("engine's own mpv is the one it drives",
              sup._client is not None and sup._client.is_running()
              and sup._client._proc.pid != orphan.pid)
        ok, _ = sup.submit_mutation({"op": "replace", "queue_version": 1,
                                     "entries": [{"id": "a", "path": song,
                                                  "title": "Song",
                                                  "source": "playlist"}]})
        check("and it plays through its own player", ok and wait_for(
            lambda: sup.status()["now_playing"] == song, 10, "song plays"))
    finally:
        sup.stop()
        for p in (orphan, bystander):
            if p.poll() is None:
                p.kill()
    print(f"STALE MPV OK ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
