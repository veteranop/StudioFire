"""Publish a StudioFire release to GitHub — the update channel every deployed
station pulls from (services/updater.py, Settings -> Software updates).

    python scripts/release.py 1.1.0 --dry-run     # show what would happen
    python scripts/release.py 1.1.0               # do it

Steps (stops at the first problem, before changing anything):
  1. On `main`, clean working tree, in sync with origin/main.
  2. 1.1.0 is newer than VERSION and every existing tag.
  3. CHANGELOG.md has entries under "## [Unreleased]" — they become the
     release notes operators see before they press Install.
  4. Runs the full test suite (tests/test_*.py) unless --no-tests.
  5. Renames the Unreleased section to "## [1.1.0] - <today>", writes
     VERSION, commits "Release v1.1.0", tags v1.1.0, pushes both, and
     creates the GitHub release (needs the `gh` CLI, logged in).

The VERSION in the tagged commit MUST equal the tag — the updater refuses a
release where they differ, so always release with this script.
Developer tool: runs only in a git checkout.
"""
import argparse
import datetime
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from services.updater import parse_version  # noqa: E402

CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")
UNRELEASED = "## [Unreleased]"


def git(*args, check=True) -> str:
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                       text=True)
    if check and r.returncode != 0:
        sys.exit(f"[!] git {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r.stdout.strip()


def fail(msg: str) -> None:
    sys.exit(f"[!] {msg}")


def split_unreleased(text: str) -> tuple[str, str, str]:
    """(before, unreleased body, after) around the [Unreleased] section."""
    i = text.find(UNRELEASED)
    if i < 0:
        fail(f"CHANGELOG.md has no '{UNRELEASED}' heading")
    start = i + len(UNRELEASED)
    m = re.search(r"^## \[", text[start:], flags=re.M)
    end = start + m.start() if m else len(text)
    return text[:i], text[start:end].strip("\n"), text[end:]


def run_tests() -> None:
    tests = sorted(f[:-3] for f in os.listdir(os.path.join(ROOT, "tests"))
                   if f.startswith("test_") and f.endswith(".py"))
    for t in tests:
        print(f"  test {t} ...", end=" ", flush=True)
        r = subprocess.run([sys.executable, "-m", f"tests.{t}"], cwd=ROOT,
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            print("FAILED")
            print((r.stdout + r.stderr)[-3000:])
            fail(f"tests.{t} failed — not releasing")
        print("ok")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("version", help="new version, e.g. 1.1.0")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-tests", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(os.path.join(ROOT, ".git")):
        fail("run this from a git checkout (developer tool)")
    new = ".".join(map(str, parse_version(args.version)))
    if new != args.version.lstrip("vV"):
        fail(f"version must look like 1.2.3 (got {args.version!r})")
    tag = "v" + new

    # 1. branch / tree / remote
    if git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        fail("releases are cut from main — merge your branch first")
    if git("status", "--porcelain", "--untracked-files=no"):
        fail("working tree has uncommitted changes")
    git("fetch", "origin", "--tags")
    if git("rev-parse", "HEAD") != git("rev-parse", "origin/main"):
        fail("main is not in sync with origin/main (pull/push first)")

    # 2. version is newer than everything
    with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
        cur = f.read().strip()
    tags = [t for t in git("tag", "--list", "v*").split() if t]
    newest = max([parse_version(cur)] + [parse_version(t) for t in tags])
    if parse_version(new) <= newest:
        fail(f"{new} is not newer than {'.'.join(map(str, newest))}")

    # 3. release notes from the changelog
    with open(CHANGELOG, encoding="utf-8") as f:
        text = f.read()
    before, notes, after = split_unreleased(text)
    if not notes.strip():
        fail("nothing under '## [Unreleased]' in CHANGELOG.md — write the "
             "release notes first (operators read them before installing)")
    today = datetime.date.today().isoformat()
    new_text = (before + UNRELEASED + "\n\n" + f"## [{new}] - {today}\n\n"
                + notes + "\n\n" + after.lstrip("\n"))

    print(f"Release {tag}  (VERSION {cur} -> {new})\n")
    print("Release notes:\n" + "-" * 60 + "\n" + notes + "\n" + "-" * 60)
    if args.dry_run:
        print("\n(dry run — nothing changed)")
        return 0

    # 4. tests
    if not args.no_tests:
        print("\nRunning tests:")
        run_tests()

    # 5. commit, tag, push, publish
    with open(CHANGELOG, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    with open(os.path.join(ROOT, "VERSION"), "w", encoding="utf-8") as f:
        f.write(new + "\n")
    git("add", "CHANGELOG.md", "VERSION")
    git("commit", "-m", f"Release {tag}")
    git("tag", "-a", tag, "-m", f"StudioFire {tag}")
    git("push", "origin", "main")
    git("push", "origin", tag)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     encoding="utf-8") as nf:
        nf.write(notes + "\n")
    r = subprocess.run(["gh", "release", "create", tag, "--title",
                        f"StudioFire {tag}", "--notes-file", nf.name,
                        "--verify-tag"], cwd=ROOT)
    os.remove(nf.name)
    if r.returncode != 0:
        fail(f"tag {tag} is pushed but `gh release create` failed — run it "
             "by hand; stations only see PUBLISHED releases")
    print(f"\n[ok] {tag} published. Stations will see it in Settings -> "
          "Software updates within a few hours (or on 'Check now').")
    return 0


if __name__ == "__main__":
    sys.exit(main())
