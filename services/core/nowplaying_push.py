"""P2 now-playing push — transport-agnostic scaffold (#747).

Mirrors StudioFire's current on-air title to the stream CDN's metadata
ingest so the public player (kdpifm.org SGplayer) shows song info. See
`docs/NOWPLAYING-CONSENSUS.md` for the locked decision.

DESIGN LAWS (non-negotiable — enforced here):
  * **P2 only.** This never runs in P1 (`services/engine/*`). P1 stays
    stdlib-only, off the network, out of the metadata path.
  * **Enqueue-only on the feeder thread.** `on_ingested()` does cheap
    in-memory work and a *non-blocking* `queue.put` — it must NEVER block
    the feeder tick (a stalled feeder starves P1's precache → failover risk).
  * **Dedicated worker thread** drains a **bounded queue** (drop-on-full) and
    does the one transport call with a **hard timeout**; it **never raises**
    and is a **silent no-op on failure** — losing a cosmetic title update is
    always preferable to any disruption.
  * **Feature-flagged, OFF by default.** When disabled this module does
    nothing; the air log is wholly independent of it.
  * **Transport is pluggable and GATED.** `DryRunTransport` (logs, zero I/O) is
    still the default. `IcecastUpdinfoTransport` is built and unit-tested
    against a local fake mount, but the feature flag stays OFF until StreamGuys
    supplies the admin credential for the mount — config alone cannot enable a
    network push by accident.

Stdlib only — no new dependencies.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod

from . import db as coredb

log = logging.getLogger("core.nowplaying_push")

# settings key for the durable "last journal id we acted on" marker — mirrors
# the `journal_ingest` offset pattern so a P2 restart never replays history to
# the CDN (which would flood it with stale titles).
SETTING_KEY = "nowplaying_push"

# default bound for the dispatch queue; track boundaries are minutes apart so
# this is enormous headroom — it exists only so a wedged transport can never
# grow memory without limit. Drop-on-full is correct: the newest title wins.
DEFAULT_QUEUE_MAX = 64


# --------------------------------------------------------------- transports

class Transport(ABC):
    """A pluggable sink for a now-playing update. `send` runs ONLY on the
    worker thread, gets a hard `timeout` budget, and may raise — the worker
    catches everything, so implementations need not swallow their own errors.
    """

    @abstractmethod
    def send(self, payload: dict, timeout: float) -> None:  # pragma: no cover
        ...


class DryRunTransport(Transport):
    """Logs the update and performs ZERO I/O. The only transport that exists
    until StreamGuys' written answer unlocks the real ones (the G0 gate)."""

    def send(self, payload: dict, timeout: float) -> None:
        log.info("nowplaying DRYRUN: song=%r source=%r journal_id=%s",
                 payload.get("song"), payload.get("source"),
                 payload.get("journal_id"))


class IcecastUpdinfoTransport(Transport):
    """Publish the on-air title to an Icecast 2 mount (`updinfo`).

    This is the transport StreamGuys named on 2026-10-09 ("send metadata to an
    Icecast 2 server"). The mount is Icecast 2.4.0-kh10 and its admin endpoint
    answers **401** unauthenticated — verified live 2026-10-24:

        GET http://peridot.streamguys.com:5110/admin/metadata -> 401

    so the shape of the call is known and only the credential is outstanding.
    Config supplies `endpoint` (scheme://host:port), `mount` (e.g. `/live`) and
    `auth` (`user:password`, env-sourced — never in config.json).

    Failure policy: this class may raise; the worker in `NowPlayingPusher`
    catches everything. A lost title update is cosmetic; it must never touch
    the audio path.
    """

    def __init__(self, endpoint: str, mount: str, auth: str | None,
                 timeout: float = 4.0):
        self.endpoint = (endpoint or "").rstrip("/")
        self.mount = mount or ""
        self.auth = auth or ""
        self.timeout = timeout

    def build_url(self, song: str) -> str:
        q = urllib.parse.urlencode({"mount": self.mount, "mode": "updinfo",
                                    "song": song})
        return f"{self.endpoint}/admin/metadata?{q}"

    @staticmethod
    def fold(song: str) -> str:
        """ASCII-fold the title before publishing.

        Icecast's `updinfo` does not reliably carry UTF-8: measured 2026-10-24
        against a real Icecast 2.4.4, `Björk – Jóga` came back out of
        `status-json.xsl` as `BjÃ¶rk â\\x80\\x93 JÃ³ga` — i.e. the server stored the
        UTF-8 bytes as Latin-1 and every listener would see mojibake. A station
        library is full of names like Björk, Mötley Crüe, Sinéad O'Connor, so
        the push folds to ASCII ("Bjork - Joga") rather than trusting the server
        to be charset-correct. A missing umlaut reads fine; `BjÃ¶rk` does not.

        Em/en dashes are turned into " - " FIRST so the artist/title separator
        survives the fold instead of being deleted with the accent.
        """
        try:
            text = song
            for dash in ("\u2014", "\u2013"):
                text = text.replace(dash, "-")
            text = unicodedata.normalize("NFKD", text)
            text = text.encode("ascii", "ignore").decode("ascii")
            return " ".join(text.split())
        except Exception:
            return song.encode("ascii", "ignore").decode("ascii").strip()

    def send(self, payload: dict, timeout: float) -> None:
        raw_song = (payload or {}).get("song")
        song = self.fold(raw_song) if raw_song else ""
        if not song:
            return
        if not self.endpoint or not self.mount or not self.auth:
            raise RuntimeError("icecast updinfo transport is not configured "
                               "(endpoint/mount/auth missing)")
        req = urllib.request.Request(self.build_url(song),
                                     headers={"User-Agent": "StudioFire-nowplaying/1"})
        token = base64.b64encode(self.auth.encode()).decode()
        req.add_header("Authorization", "Basic " + token)
        budget = min(float(timeout or self.timeout), self.timeout)
        with urllib.request.urlopen(req, timeout=budget) as resp:
            body = resp.read(256)
        # Icecast answers "Metadata update successful"; anything that is not a
        # 2xx has already raised, so this is only for the log.
        log.debug("icecast updinfo ok: %r -> %r", song, body[:80])


def build_transport(np_cfg: dict) -> Transport:
    """Factory: `config.transport` selects. Real transports are gated on the
    vendor's answer, so an unconfigured one falls back to DryRun (with a
    warning) rather than attempting a network path that isn't ratified."""
    name = (np_cfg or {}).get("transport", "dryrun")
    if name in ("icecast", "icecast-updinfo"):
        cfg = np_cfg or {}
        if cfg.get("endpoint") and cfg.get("mount") and cfg.get("auth"):
            return IcecastUpdinfoTransport(cfg["endpoint"], cfg["mount"],
                                           cfg["auth"],
                                           float(cfg.get("timeout_sec", 4.0)))
        log.warning("nowplaying transport 'icecast' selected but endpoint/mount/"
                    "auth are not all set — using DryRunTransport")
        return DryRunTransport()
    if name == "dryrun":
        return DryRunTransport()
    log.warning("nowplaying transport %r not available yet (gated on "
                "StreamGuys) — using DryRunTransport", name)
    return DryRunTransport()


# ---------------------------------------------------------------- the pusher

class NowPlayingPusher:
    """Fire-and-forget now-playing pusher. Build with `from_config`, call
    `start()` once, then `on_ingested(conn, events, now_playing)` from the
    feeder loop after each `ingest_journal`. `stop()` on shutdown."""

    def __init__(self, np_cfg: dict, transport: Transport | None = None,
                 queue_max: int = DEFAULT_QUEUE_MAX):
        self.enabled = bool((np_cfg or {}).get("enabled", False))
        self.transport = transport or build_transport(np_cfg)
        self.timeout_sec = float((np_cfg or {}).get("timeout_sec", 4.0))
        self.min_interval_sec = float((np_cfg or {}).get("min_interval_sec", 5.0))
        self.include_spots = bool((np_cfg or {}).get("include_spots", False))

        self._q: queue.Queue = queue.Queue(maxsize=max(1, int(queue_max)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # feeder-thread-only in-memory state (single writer: the feeder loop)
        self._loaded = False
        self._last_jid_str: str | None = None
        self._last_jid_key: tuple[int, int] = (-1, -1)  # (-1,-1) ⇒ nothing seen
        self._last_title: str | None = None
        self._last_push_mono: float | None = None

    @classmethod
    def from_config(cls, cfg: dict) -> "NowPlayingPusher":
        """Build from the full P2 config dict (reads its `nowplaying_push`)."""
        return cls(cfg.get("nowplaying_push", {}) or {})

    # ------------------------------------------------------ lifecycle

    def start(self) -> None:
        if not self.enabled:
            log.info("nowplaying push disabled (flag off) — not starting worker")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="nowplaying-push", daemon=True)
        self._thread.start()
        log.info("nowplaying push worker started (transport=%s)",
                 type(self.transport).__name__)

    def stop(self, join_timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=join_timeout)

    # ------------------------------------------- feeder-thread entry point

    def on_ingested(self, conn, events, now_playing: str | None = None) -> int:
        """Called on the FEEDER thread with the `track_start` events just
        ingested this tick (each a journal event dict) and the engine's live
        `now_playing` path from /status, if known. Enqueue-only; returns the
        number of updates enqueued (0 or 1). NEVER blocks and NEVER raises.
        """
        try:
            if not self.enabled:
                return 0
            self._ensure_loaded(conn)
            # only events strictly newer than the durable marker — this is the
            # no-replay guard across P2 restarts
            fresh = [e for e in (events or [])
                     if self._jid_newer(e.get("id"))]
            if not fresh:
                return 0
            newest = max(fresh, key=lambda e: self._jid_key(e.get("id")))
            # advance + persist the marker past ALL fresh events so the ones we
            # deliberately drop below never resurface on a later tick/restart
            self._set_marker(conn, newest.get("id"))
            # reconcile to the CURRENT track: on a backlog drain (P2 was down)
            # many track_starts arrive at once — only the one actually on air
            # matters. Prefer the event whose path matches the engine's live
            # now_playing; otherwise the newest. Stale ones are dropped.
            cur = None
            if now_playing:
                cur = next((e for e in fresh
                            if e.get("path") == now_playing), None)
            if cur is None:
                cur = newest
            if not self._should_push(cur):
                return 0
            self._enqueue(self._payload(cur))
            return 1
        except Exception:  # feeder tick must never die on our account
            log.warning("nowplaying on_ingested failed (non-fatal)",
                        exc_info=True)
            return 0

    # ------------------------------------------------------ internals

    def _should_push(self, ev: dict) -> bool:
        """Content filters (feeder thread). Mutates dedup/throttle state only
        when it decides to push."""
        title = ev.get("title")
        if title is None:
            return False  # emergency filler / baked-in / untitled ID
        if ev.get("source") == "spot" and not self.include_spots:
            return False
        if title == self._last_title:
            return False  # dedup consecutive identical titles
        now = time.monotonic()
        if (self._last_push_mono is not None
                and (now - self._last_push_mono) < self.min_interval_sec):
            return False  # throttle — don't hammer the CDN
        self._last_title = title
        self._last_push_mono = now
        return True

    @staticmethod
    def _payload(ev: dict) -> dict:
        # the journal `title` is already a single "Artist - Song" string, which
        # is the exact shape an Icecast `updinfo` StreamTitle wants. Split
        # artist/title/art is a flexible, later concern (resolve like /api/queue).
        return {"journal_id": ev.get("id"),
                "title": ev.get("title"),
                "source": ev.get("source"),
                "song": ev.get("title")}

    def _enqueue(self, payload: dict) -> None:
        try:
            self._q.put_nowait(payload)
        except queue.Full:
            # drop-on-full: a backed-up queue means the transport is wedged;
            # the freshest title is the only one worth keeping, and we never
            # block the feeder waiting for room.
            log.debug("nowplaying queue full — dropping %r", payload.get("song"))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.transport.send(payload, self.timeout_sec)
            except Exception:
                # silent no-op on failure (per the design law): log and move on
                log.warning("nowplaying push failed (non-fatal)", exc_info=True)
            finally:
                self._q.task_done()

    # ------------------------------------------------------ marker (durable)

    def _ensure_loaded(self, conn) -> None:
        if not self._loaded:
            self.load_state(conn)

    def load_state(self, conn) -> None:
        raw = coredb.get_setting(conn, SETTING_KEY)
        if raw:
            try:
                jid = json.loads(raw).get("last_journal_id")
            except (ValueError, AttributeError):
                jid = None
            if jid:
                self._last_jid_str = jid
                self._last_jid_key = self._jid_key(jid)
        self._loaded = True

    def _set_marker(self, conn, jid) -> None:
        if not jid:
            return
        self._last_jid_str = jid
        self._last_jid_key = self._jid_key(jid)
        coredb.set_setting(conn, SETTING_KEY,
                           json.dumps({"last_journal_id": jid}))

    def _jid_newer(self, jid) -> bool:
        return self._jid_key(jid) > self._last_jid_key

    @staticmethod
    def _jid_key(jid) -> tuple[int, int]:
        """Journal ids are strings `"<ms>-<seq>"` (see engine/journal.py). Parse
        to an (ms, seq) tuple for correct chronological ordering — `seq` resets
        on each engine restart, but the ms prefix dominates across restarts and
        track_starts are minutes apart, so ordering is unambiguous."""
        if not jid or not isinstance(jid, str):
            return (-1, -1)
        try:
            ms, seq = jid.split("-", 1)
            return (int(ms), int(seq))
        except ValueError:
            return (-1, -1)
