"""Drive-letter-independent path resolution (shared, stdlib-only).

Why this exists
---------------
A mapped drive (e.g. Z:) is a PER-LOGON-SESSION property. It is not inherited
by a detached process, and it is gone after a reboot until something re-runs
`net use`. StudioFire's database is full of Z:\\-rooted paths — playlists, spot
folders, station-ID folders, the .lst mirror dir — so the moment that mapping
is missing, every stored path fails os.path.isdir/isfile and legal IDs, PSAs,
ads and shows silently miss their windows (KDPI, 2026-09-29: 132 spot misses
after a Windows-Update reboot dropped Z:).

The fix: a drive letter must NEVER be required to resolve a path. A path on a
drive that does not exist in this session is re-pointed at the configured NAS
share root, derived from nas_music_root. Explicit path_aliases always win.

Kept dependency-free (os only) so the engine (P1, stdlib-only by design) can
import it too, not just core.
"""

from __future__ import annotations

import os

# "Which drive letters exist" is a per-session fact that does not change while
# the process lives, and resolve() sits in the feeder's hot loop — so probe each
# letter at most once. This cache is the whole reason the fallback is cheap.
_DRIVE_EXISTS: dict[str, bool] = {}


def _drive_exists(letter: str) -> bool:
    key = letter.upper()
    hit = _DRIVE_EXISTS.get(key)
    if hit is None:
        hit = os.path.exists(key + ":\\")
        _DRIVE_EXISTS[key] = hit
    return hit


def clear_drive_cache() -> None:
    """Forget the probed drive letters. For tests, and safe to call if a
    mapping is added/removed mid-run and you want it re-probed."""
    _DRIVE_EXISTS.clear()


def _drive_letter(path: str) -> str | None:
    """'Z' for 'Z:\\...' or 'Z:/...'; None for UNC / relative / empty paths."""
    if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
        return path[0]
    return None


def share_root(nas_music_root: str | None) -> str:
    """The NAS share root (\\\\host\\share) that a mapped drive stands in for.

    config\\drive-map.bat maps Z: -> \\\\KDPI-Media\\music while nas_music_root
    is //KDPI-Media/music/G, so the share root is nas_music_root minus its last
    path component. Accepts // or \\\\ UNC input and always returns the
    canonical Windows form (\\\\host\\share, backslashes) — that is what is
    already stored inside the .lst files."""
    root = (nas_music_root or "").strip()
    if not root:
        return ""
    unc = root.replace("/", "\\").rstrip("\\")
    # Keep the form we were given: UNC stays UNC, a drive/local root stays
    # drive/local. Then strip the last component — but only if there IS one:
    # \\host\share and Z: are already roots, and stripping them would produce a
    # wrong (or outright bogus) target instead of a harmless no-op.
    kind = "\\\\" if unc.startswith("\\\\") else ""
    body = unc[2:] if kind else unc
    need = 2 if kind else 1          # UNC needs host\share\x; a drive needs X:\x
    if body.count("\\") < need:
        return kind + body
    return kind + body.rsplit("\\", 1)[0]


def resolve(path: str, aliases: dict | None = None,
            nas_root: str | None = None) -> str:
    """Rewrite `path` so a missing drive letter never breaks resolution.

    1. Explicit path_aliases win — a case-insensitive prefix swap, e.g. a .lst
       written at the studio as \\\\SERVER\\share\\... mapped back to Z:\\...
    2. Otherwise, if the path is on a drive letter that does NOT exist in this
       session, re-point it under the NAS share root derived from nas_root:
         Z:\\John\\New PSAs  ->  \\\\KDPI-Media\\music\\John\\New PSAs
       A path on a drive that DOES exist (e.g. C:\\...) is left untouched, and
       so is any path when no NAS root is configured.
    """
    if not path:
        return path
    for prefix, repl in (aliases or {}).items():
        if prefix and path.lower().startswith(prefix.lower()):
            return repl + path[len(prefix):]
    letter = _drive_letter(path)
    if letter and not _drive_exists(letter):
        root = share_root(nas_root)
        if root:
            # drop 'X:' + any leading separator, and normalise what's left to
            # backslashes so a stored "Z:/John/New PSAs" resolves to a canonical
            # UNC path (mixed separators work on Windows but read badly, and
            # these strings get compared and written back into .lst files)
            rest = path[2:].lstrip("\\/").replace("/", "\\")
            return root + "\\" + rest if rest else root
    return path
