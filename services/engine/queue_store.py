"""Atomic queue persistence + version protocol for P1. PLAN.md §10.2.

P1 design laws apply: STDLIB ONLY. Single writer: only the P1 supervisor
thread ever calls save(). P2 never touches this file.

Persisted shape (one JSON file):
    {
      "queue_version": 42,          # monotonic; set by P2 mutations
      "entries": [ {"id": "...", "path": "C:/.../song.mp3", "title": "...",
                    "source": "playlist|spot|manual|emergency"}, ... ],
      "current_index": 3,           # -1 = nothing started yet
      "emergency_mode": false,      # restart must re-enter the right mode (§10.2)
      "timestamp": 1751500000.0
    }

Guarantees:
- save() is atomic (temp file + os.replace) and durable (fsync) — a crash
  mid-write can never corrupt the previous good state.
- load() never raises on bad/missing state: a corrupt file is quarantined
  to .corrupt-<ts> for post-mortem and a safe empty state (emergency_mode
  True) is returned. Empty queue -> supervisor goes to emergency -> air
  stays on. Bad state must never prevent startup.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

log = logging.getLogger("engine.queue")


@dataclass
class QueueState:
    queue_version: int = 0
    entries: list = field(default_factory=list)
    current_index: int = -1
    emergency_mode: bool = False
    forced_emergency: bool = False  # operator hit the EMERGENCY button
    timestamp: float = 0.0

    def current_entry(self):
        if 0 <= self.current_index < len(self.entries):
            return self.entries[self.current_index]
        return None

    def next_entry(self):
        nxt = self.current_index + 1
        if 0 <= nxt < len(self.entries):
            return self.entries[nxt]
        return None

    def trim_history(self, keep: int) -> int:
        """Drop already-played entries older than the last `keep`, so the queue
        can't grow without bound over a long broadcast (the play journal is the
        permanent as-aired record; this is just runtime memory). Everything from
        the currently-playing entry onward (current + all pending) is untouched.
        Returns how many were dropped (current_index shifts down by that)."""
        if self.current_index <= keep:
            return 0
        drop = self.current_index - keep
        del self.entries[:drop]
        self.current_index -= drop
        return drop

    def to_dict(self) -> dict:
        return {
            "queue_version": self.queue_version,
            "entries": self.entries,
            "current_index": self.current_index,
            "emergency_mode": self.emergency_mode,
            "forced_emergency": self.forced_emergency,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QueueState":
        state = cls(
            queue_version=int(d["queue_version"]),
            entries=list(d["entries"]),
            current_index=int(d["current_index"]),
            emergency_mode=bool(d["emergency_mode"]),
            forced_emergency=bool(d.get("forced_emergency", False)),
            timestamp=float(d.get("timestamp", 0.0)),
        )
        for e in state.entries:  # structural validation, fail loud -> quarantine
            if not isinstance(e, dict) or "path" not in e:
                raise ValueError(f"malformed queue entry: {e!r}")
        if not (-1 <= state.current_index <= len(state.entries)):
            raise ValueError(f"current_index {state.current_index} out of range")
        return state


class QueueStore:
    """Owns the queue state file. One instance, one writer thread."""

    def __init__(self, path: str):
        self._path = path
        self._tmp_path = path + ".tmp"

    def save(self, state: QueueState) -> None:
        """Atomic + durable. Called on every mutation and every advance."""
        state.timestamp = time.time()
        data = json.dumps(state.to_dict(), indent=1).encode("utf-8")
        with open(self._tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(self._tmp_path, self._path)

    def load(self) -> QueueState:
        """Never raises. Corrupt state is quarantined, safe default returned."""
        if not os.path.exists(self._path):
            log.info("no queue state at %s — fresh start", self._path)
            return QueueState(emergency_mode=True)
        try:
            with open(self._path, "rb") as f:
                return QueueState.from_dict(json.loads(f.read()))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            quarantine = f"{self._path}.corrupt-{int(time.time())}"
            log.error("queue state corrupt (%s) — quarantined to %s", exc, quarantine)
            try:
                os.replace(self._path, quarantine)
            except OSError:
                pass
            return QueueState(emergency_mode=True)


def apply_mutation(state: QueueState, mutation: dict) -> tuple[bool, str]:
    """Apply a P2 queue mutation to state, enforcing the version protocol.

    Mutation shape: {"op": ..., "queue_version": <new version>, ...}
    Rules (§10.2): new version must be > current, else rejected (P2 re-syncs).
    Returns (accepted, reason). Caller (supervisor) persists on accept.
    """
    new_version = mutation.get("queue_version")
    if not isinstance(new_version, int) or new_version <= state.queue_version:
        return False, (f"stale version {new_version!r} "
                       f"(current {state.queue_version})")

    op = mutation.get("op")
    if op == "replace":
        entries = mutation.get("entries", [])
        state.entries = entries
        state.current_index = -1 if entries else -1
    elif op == "append":
        state.entries.extend(mutation.get("entries", []))
    elif op == "insert_next":
        pos = min(state.current_index + 1, len(state.entries))
        state.entries[pos:pos] = mutation.get("entries", [])
    elif op == "clear_pending":
        # drop everything after the currently playing item
        state.entries = state.entries[: state.current_index + 1]
    elif op == "reorder":
        # reorder ONLY the pending tail (never the played/current entries).
        # order = desired id sequence; any pending id not named keeps its
        # place at the end (defensive against a stale client view).
        order = mutation.get("order", [])
        cut = state.current_index + 1
        head, tail = state.entries[:cut], state.entries[cut:]
        by_id = {e.get("id"): e for e in tail}
        named = [by_id[i] for i in order if i in by_id]
        mentioned = set(order)
        rest = [e for e in tail if e.get("id") not in mentioned]
        state.entries = head + named + rest
    elif op == "remove":
        # remove pending entries by id; current/played entries are immune
        ids = set(mutation.get("ids", []))
        cut = state.current_index + 1
        state.entries = (state.entries[:cut]
                         + [e for e in state.entries[cut:]
                            if e.get("id") not in ids])
    else:
        return False, f"unknown op {op!r}"

    state.queue_version = new_version
    return True, "ok"
