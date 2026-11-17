"""The release publishes a verifiable installer (M2632104 follow-up, 2026-11-17).

The README told operators "checksums are published with each release" while no
release carried one — a promise about *trust*, in the paragraph that asks them
to click past SmartScreen on an unsigned .exe. `release.py` now writes
`StudioFire-Setup-<v>.exe.sha256`, attaches it to the release and prints the
hash in the notes.

What these checks pin:
  * the hash is the real SHA-256 of the .exe (streamed — the installer is 56 MB)
  * the file is the standard `sha256sum -c` shape, ASCII, LF, one line
  * the notes block carries the digest, the file name and the Windows command
    an operator actually has (`certutil -hashfile`)
  * the publish call passes BOTH the exe and the checksum file (a checksum that
    is written but never attached is the bug this exists to prevent)

Run: python tests/test_release_checksum.py
"""
import hashlib
import importlib.util
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  :", name)


def load_release():
    """Import scripts/release.py as a module (it is a script, not a package).

    main() is guarded by __main__, so importing it defines helpers and nothing
    else; no git, no build, no network.
    """
    path = os.path.join(ROOT, "scripts", "release.py")
    spec = importlib.util.spec_from_file_location("sf_release", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    rel = load_release()

    td = tempfile.mkdtemp(prefix="sf-relchk-")
    try:
        # ---- the hash is real, including across read-chunk boundaries ----
        big = os.path.join(td, "StudioFire-Setup-9.9.9.exe")
        payload = bytes(range(256)) * 8192          # 2 MB: exercises streaming
        with open(big, "wb") as fh:
            fh.write(payload)
        expect = hashlib.sha256(payload).hexdigest()
        check("sha256_of matches hashlib on a multi-chunk file",
              rel.sha256_of(big) == expect)
        check("the digest is 64 lowercase hex characters",
              len(expect) == 64 and expect == expect.lower()
              and all(c in "0123456789abcdef" for c in expect))

        # ---- the checksum file's shape -----------------------------------
        digest, out = rel.write_checksum(big)
        check("write_checksum returns its own digest", digest == expect)
        check("the file sits beside the installer, named .sha256",
              out == big + ".sha256" and os.path.isfile(out))
        raw = open(out, "rb").read()
        check("ASCII, LF-only, one line",
              b"\r" not in raw and raw.endswith(b"\n")
              and raw.count(b"\n") == 1
              and raw.decode("ascii") is not None)
        line = raw.decode("ascii")
        check("it is the `sha256sum -c` shape: '<hash>  <filename>'",
              line.rstrip("\n") == f"{expect}  StudioFire-Setup-9.9.9.exe")
        # what `sha256sum -c` would do with it
        h, name = line.split("  ")
        check("sha256sum -c would verify this file",
              name.strip() == os.path.basename(big)
              and hashlib.sha256(open(big, "rb").read()).hexdigest() == h)

        # ---- the wording operators see -----------------------------------
        block = rel.checksum_block(expect, "StudioFire-Setup-9.9.9.exe")
        check("the notes block carries the digest", expect in block)
        check("it names the installer", "StudioFire-Setup-9.9.9.exe" in block)
        check("it gives the Windows command an operator actually has",
              "certutil -hashfile" in block)
        check("it mentions the attached .sha256 for sha256sum -c",
              "StudioFire-Setup-9.9.9.exe.sha256" in block
              and "sha256sum -c" in block)
        check("it appends cleanly below the changelog notes",
              block.startswith("\n\n---\n\n"))

        # ---- a checksum that is written but not published is the bug ------
        src = open(os.path.join(ROOT, "scripts", "release.py"),
                   encoding="utf-8").read()
        i = src.find('"gh", "release", "create"')
        argv = src[i:i + 400] if i >= 0 else ""
        check("the publish call passes the exe AND the checksum file",
              i >= 0 and "exe, chk" in argv)
        check("the notes sent to GitHub are the ones with the checksum",
              'nf.write(notes_with_checksum + "\\n")' in src)
        check("the checksum is not baked into CHANGELOG.md",
              "checksum_block" not in src.split("new_text = (")[1].split(")")[0])
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print(f"\n{passed} checks passed")


if __name__ == "__main__":
    main()
