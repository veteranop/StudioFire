"""Tag fields the stream vendor needs but our DB does not store.

WHY THIS EXISTS
---------------
StreamGuys builds the SoundExchange report of use straight off the ICY
StreamTitle we publish, and their recommended field order is

    artist | title | album | marketing label | isrc

`tracks` carries artist and album (~97% populated) but has **no isrc and no
label column** — those exist only inside the audio file's own tags. The
now-playing line is written on every track change, so this reads the tags off
the LOCAL precache copy (never over SMB: the engine has already copied the file
to local disk before it plays).

DESIGN LAWS
-----------
* **Never raises.** A missing file, a locked file, an unsupported container or
  an absent mutagen must all come back as empty strings — the caller is the
  feeder thread, and a tag read may never cost a title or a tick.
* **Cached per (path, mtime, size).** The feeder can re-write the same title
  (a P2 restart, a manual "write the current song now"), and re-opening a file
  for an unchanging answer is pure waste on the audio box.
* **Same key sweep as `sx_report.read_tags`**, so the on-air line and the
  report agree about which tag means "label". Container vocabularies differ:
  ID3 (`TSRC`, `TPUB`), Vorbis (`isrc`, `label`/`organization`/`publisher`),
  MP4 (`----:com.apple.iTunes:ISRC`, `©pub`/`©lab`).

Deliberately NOT a database column: a schema change plus a full-library
backfill is a separate job (and the reporting rule accepts album + marketing
label in place of ISRC), so tagging stays a read-time concern.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("core.tagfields")

# Bounded: a station never has more than a few hundred distinct tracks in a
# day's rotation; clearing on overflow is cheaper than tracking recency.
_CACHE: dict = {}
_CACHE_MAX = 512

EMPTY = {"album": "", "label": "", "isrc": ""}


def _first(val) -> str:
    """Tags arrive as str, list, tuple, ID3 frame objects… take one plain str."""
    try:
        if isinstance(val, (list, tuple)):
            val = val[0] if val else ""
        v = getattr(val, "text", val)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else ""
        return " ".join(str(v).split()).strip()
    except Exception:
        return ""


def read(path: str) -> dict:
    """`{'album','label','isrc'}` from a local audio file's tags. Never raises."""
    if not path:
        return dict(EMPTY)
    try:
        st = os.stat(path)
        key = (int(st.st_mtime), int(st.st_size))
    except OSError:
        # Not on local disk (or gone): answer empty, do not cache — the file may
        # appear on the next pass.
        return dict(EMPTY)

    hit = _CACHE.get(path)
    if hit and hit[0] == key:
        return dict(hit[1])

    out = dict(EMPTY)
    try:
        import mutagen  # lazy: optional dependency, never fatal
        f = mutagen.File(path, easy=False)
    except Exception:
        f = None
    if f is not None:
        try:
            tags = f.tags
            items = list(tags.items()) if tags is not None else []
        except Exception:
            items = []
        for tag_key, val in items:
            k = str(tag_key).upper()
            v = _first(val)
            if not v:
                continue
            if not out["isrc"] and ("ISRC" in k or k == "TSRC"):
                out["isrc"] = v
            elif not out["label"] and ("TPUB" in k or "LABEL" in k
                                       or "ORGANIZATION" in k
                                       or "PUBLISHER" in k
                                       or "\u00a9PUB" in k or "\u00a9LAB" in k):
                out["label"] = v
            elif not out["album"] and k in ("TALB", "ALBUM", "\u00a9ALB"):
                out["album"] = v
    # `easy=False` on MP4/ID3 gives the raw frame names above; an `album` that
    # only the easy layer knows about is picked up here as a fallback.
    if not out["album"]:
        try:
            import mutagen
            easy = mutagen.File(path, easy=True)
            if easy is not None and easy.tags is not None:
                out["album"] = _first(easy.tags.get("album"))
        except Exception:
            pass

    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[path] = (key, dict(out))
    return out
