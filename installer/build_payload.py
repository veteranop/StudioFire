"""Prepare the installer payload: embedded Python runtime + NSSM.

Run this ONCE on the build box (needs internet) before compiling
StudioFire.iss with Inno Setup 6:

    python installer\build_payload.py

Produces:
    installer\payload\runtime\   - python.org embeddable 3.12 + pip + our deps
    installer\payload\nssm.exe   - service manager for the on-air PC

The runtime is fully self-contained and relocatable: python312._pth gets
".." appended so the interpreter always sees the install root (where
services/ lives) on sys.path, no matter where the app is installed and
regardless of the working directory. No system Python, no PATH edits,
no Windows-Store-stub landmine (see start-all.bat).

mpv.exe is NOT downloaded here — copy your known-good bin\mpv.exe into
the repo's bin\ (the .iss picks it up from there).
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

PYTHON_VERSION = "3.12.8"
PYTHON_URL = ("https://www.python.org/ftp/python/{v}/"
              "python-{v}-embed-amd64.zip").format(v=PYTHON_VERSION)
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
NSSM_URL = "https://nssm.cc/release/nssm-2.24.zip"

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PAYLOAD = os.path.join(HERE, "payload")
RUNTIME = os.path.join(PAYLOAD, "runtime")


def fetch(url: str) -> bytes:
    print(f"  downloading {url}")
    with urllib.request.urlopen(url) as r:
        return r.read()


def build_runtime() -> None:
    if os.path.isdir(RUNTIME):
        print(f"[runtime] removing stale {RUNTIME}")
        shutil.rmtree(RUNTIME)
    os.makedirs(RUNTIME)

    print(f"[runtime] embedded Python {PYTHON_VERSION}")
    with zipfile.ZipFile(io.BytesIO(fetch(PYTHON_URL))) as z:
        z.extractall(RUNTIME)

    # Path file: keep the stdlib entries, enable site (pip needs it), and
    # add ".." = the install root, so `python -m services.engine.main`
    # works from any cwd (the ._pth suppresses cwd-on-sys.path entirely).
    pth = next(f for f in os.listdir(RUNTIME) if f.endswith("._pth"))
    pth_path = os.path.join(RUNTIME, pth)
    with open(pth_path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    lines = [ln for ln in lines if ln != "#import site"]
    lines += ["..", "import site"]
    with open(pth_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[runtime] patched {pth}: {lines}")

    py = os.path.join(RUNTIME, "python.exe")

    print("[runtime] bootstrapping pip")
    get_pip = os.path.join(RUNTIME, "get-pip.py")
    with open(get_pip, "wb") as f:
        f.write(fetch(GET_PIP_URL))
    subprocess.run([py, get_pip, "--no-warn-script-location"], check=True)
    os.remove(get_pip)

    print("[runtime] installing requirements.txt")
    subprocess.run([py, "-m", "pip", "install", "--no-warn-script-location",
                    "-r", os.path.join(REPO, "requirements.txt")], check=True)

    print("[runtime] sanity import check")
    subprocess.run([py, "-c",
                    "import fastapi, uvicorn, jinja2, apscheduler, mutagen, "
                    "watchdog, paramiko, httpx, pydantic; print('deps OK')"],
                   check=True)


def build_nssm() -> None:
    dest = os.path.join(PAYLOAD, "nssm.exe")
    if os.path.isfile(dest):
        print("[nssm] already present")
        return
    print("[nssm] 2.24")
    with zipfile.ZipFile(io.BytesIO(fetch(NSSM_URL))) as z:
        with z.open("nssm-2.24/win64/nssm.exe") as src, open(dest, "wb") as f:
            shutil.copyfileobj(src, f)


def main() -> int:
    os.makedirs(PAYLOAD, exist_ok=True)
    build_runtime()
    build_nssm()
    mpv = os.path.join(REPO, "bin", "mpv.exe")
    if not os.path.isfile(mpv):
        print(f"\n[!] {mpv} missing — the .iss needs it. Copy your "
              "known-good mpv.exe there before compiling.")
        return 1
    print("\nPayload ready. Now compile with Inno Setup 6:\n"
          '  "C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe" '
          "installer\\StudioFire.iss")
    return 0


if __name__ == "__main__":
    sys.exit(main())
