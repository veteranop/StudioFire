"""Parse CHANGELOG.md for the Settings "What's new / Change log" section.

CHANGELOG.md is a managed file (ships in every release, see
services/updater.py MANAGED_FILES), so this reads what's already on disk — no
new file has to ship. STDLIB ONLY.

We parse the Keep-a-Changelog shape as it's actually written in this repo:

    ## [1.2.0] - 2026-09-29      <- a released version + its date
    ## [Unreleased]              <- not a released version
    ### Fixed                    <- a group heading (Fixed/Added/Changed…)
    - **A bold lead-in.** then    <- a bullet, often wrapped across several
      the rest of the sentence      lines; we join the continuation lines
      wraps onto more lines.        back into ONE bullet.

Bullets wrap across lines in the source (so the file stays readable in an
editor). If we didn't join them, every entry would look truncated at the first
line break — so a continuation line (a non-blank line that isn't a new bullet,
a heading or a version header) is appended to the current bullet with a single
space.

The parser is deliberately forgiving: a missing or garbage file yields
``{"ok": False, ...}`` rather than raising, so the Settings page can always
render (an older install may have no CHANGELOG at all).
"""

from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# "## [1.2.0] - 2026-09-29", "## [Unreleased]", "## [1.0.0]"
_VERSION_RE = re.compile(r"^\[(?P<ver>[^\]]+)\]\s*(?:[-–—]\s*(?P<date>.+))?$")
# a **bold lead-in** inside an entry (non-greedy, may span the whole entry)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _is_bullet(stripped: str) -> bool:
    return stripped[:2] in ("- ", "* ")


def parse(text: str) -> list[dict]:
    """Parse changelog `text` into a list of releases, newest first (i.e. in
    the order they appear in the file). Each release is::

        {"version": "1.2.0", "date": "2026-09-29", "released": True,
         "groups": [{"heading": "Fixed", "intro": "", "entries": [str, …]}]}

    ``released`` is False for the ``[Unreleased]`` section. Wrapped bullets are
    joined into a single string per entry.
    """
    releases: list[dict] = []
    rel: dict | None = None
    group: dict | None = None
    entry_open = False        # are we currently extending a bullet?

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if not stripped:
            entry_open = False           # a blank line ends the current bullet
            continue

        if line.startswith("### "):                       # group heading
            if rel is not None:
                group = {"heading": line[4:].strip(), "intro": "",
                         "entries": []}
                rel["groups"].append(group)
                entry_open = False
            continue

        if line.startswith("## "):                        # version header
            content = line[3:].strip()
            m = _VERSION_RE.match(content)
            ver = (m.group("ver") if m else content).strip()
            date = (m.group("date").strip() if m and m.group("date") else "")
            rel = {"version": ver, "date": date,
                   "released": ver.lower() != "unreleased", "groups": []}
            releases.append(rel)
            group = None
            entry_open = False
            continue

        if line.startswith("#"):          # the file title / other headings
            continue

        if rel is None:                    # preamble before the first version
            continue

        if _is_bullet(stripped):           # a new bullet
            if group is None:              # bullets with no ### above them
                group = {"heading": "", "intro": "", "entries": []}
                rel["groups"].append(group)
            group["entries"].append(stripped[2:].strip())
            entry_open = True
            continue

        # a non-blank, non-bullet line: continuation of a bullet, or intro text
        if entry_open and group and group["entries"]:
            group["entries"][-1] += " " + stripped
        elif group is not None:            # text between a ### and its bullets
            group["intro"] = (group["intro"] + " " + stripped).strip() \
                if group["intro"] else stripped

    return releases


def load(root: str | None = None) -> dict:
    """Read CHANGELOG.md from the install root and parse it. Never raises::

        {"ok": True,  "error": None, "releases": [...]}
        {"ok": False, "error": "…", "releases": []}
    """
    path = os.path.join(root or ROOT, "CHANGELOG.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        return {"ok": False, "error": f"no changelog on this install ({exc})",
                "releases": []}
    try:
        releases = parse(text)
    except Exception as exc:  # noqa: BLE001 — a parse hiccup must never 500
        return {"ok": False, "error": f"couldn't read the changelog ({exc})",
                "releases": []}
    if not any(r["released"] for r in releases):
        return {"ok": False, "error": "no released versions found",
                "releases": releases}
    return {"ok": True, "error": None, "releases": releases}


def bold_segments(text: str) -> list[list]:
    """Split an entry into ``[segment, is_bold]`` pairs on ``**bold**`` markers.

    Used by the template so a bold lead-in renders as <strong> WITHOUT any
    manual HTML building — each segment is still auto-escaped by Jinja, so
    backslashes (``Z:\\`` paths etc.) survive intact and nothing in the source
    text can inject markup.
    """
    out: list[list] = []
    for idx, part in enumerate(_BOLD_RE.split(text)):
        if part == "":
            continue
        out.append([part, idx % 2 == 1])       # odd chunks are the bold groups
    return out
