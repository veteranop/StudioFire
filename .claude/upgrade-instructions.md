# StudioFire Feeder Hardening — Execution Instructions

**Written by:** Claude (Fable 5) after architecture review, 2026-09-23
**Intended executor:** Claude Sonnet (all design decisions are pinned below — execute, don't redesign)
**TimeTrax ticket:** #634 — "StudioFire feeder hardening (concurrency + buffer decoupling)" (client: VeteranOp)
**Risk level:** HIGH — this is production on-air software for a live FM station. Work on a branch. Do NOT deploy. Do NOT push to GitHub.

---

## Ground rules (read first)

1. `git checkout -b feeder-hardening` before touching anything.
2. **Do not modify anything in `services/engine/`** (P1, the audio engine). Every change in this spec lives in `services/core/`, `tests/`, `config/config.example.json`, and `.claude/CLAUDE.md`. If you believe a P1 change is needed, STOP and write the reason into `REVIEW.md` instead of making it.
3. Do not touch `config/config.json` (live config). Only `config.example.json`.
4. Run the full test suite before AND after (see Task 5). All-green is the bar; if something fails before you start, record it in `REVIEW.md` and continue.
5. Commit locally in logical chunks. Do not `git push`. Do not run `start-all.bat`, `restart-all.bat`, or anything under `scripts/` that launches services.
6. When done: update `CHANGELOG.md` (Unreleased section), append a summary to `REVIEW.md`, and leave the branch for human + Opus review.

## Background (why)

Architecture review found one real defect and three smaller items in P2 (`services/core/engine_bridge.py`):

- **Defect — lost-update race on feeder state.** `Feeder` bookkeeping (`feeder_state` in SQLite settings) is read-modify-written with no lock. The background feeder loop (`loop()` at the bottom of `engine_bridge.py`, every 5s) holds a stale in-memory copy across slow NAS copies inside `tick()`, while FastAPI request threads (`insert_spot`, the `/api/engine/play_next` endpoint, `activate`, `resync_rotation`, show ops) load/mutate/save the same state concurrently. The stale save wins, the operator's entry vanishes from `st["fed"]`, then `_evict()` deletes its cached file while it is still pending in P1's queue → P1 skips it as "unplayable at prefetch". The DJ's cued track or a fired ad silently never airs.
- **Design fix — feed depth vs cache depth are conflated.** The 45-minute target governs both how much is cached on disk AND how deep P1's pending queue is. Only the disk cache needs 45 min (NAS-outage armor + P1's emergency filler tier reads the precache dir). A deep P1 queue just makes the UI lag reality by ~45 min. Decouple them: feed P1 only a few tracks ahead; keep 45 min cached on disk via a lookahead.
- **Spot affidavit gap.** `fire_due_spots` calls `spotmod.mark_fired` even when insertion failed — a transient failure means the ad never airs in its window and never retries.
- **Stale docs.** `.claude/CLAUDE.md` references `python main.py` (doesn't exist) and a "KDPI protocol" (KDPI is the station's call sign, not a protocol).

---

## Task 1 — Serialize all feeder state access

**File:** `services/core/engine_bridge.py`

### 1a. Add the lock

In `Feeder.__init__`, add:

```python
self._lock = threading.RLock()   # serializes ALL feeder_state read-modify-write
```

RLock (not Lock) because public methods call each other (e.g. `stop_show` → `tick`, `reorder_show` → `_resync_show` → `tick`).

### 1b. Wrap every load→save critical section

Every method that calls `self._load_state(...)` must hold `self._lock` from before the load until after the final `_save_state` / `tick` call it makes. Methods to wrap (wrap the whole method body unless stated otherwise):

- `activate`
- `stop_show`
- `start_show_now`
- `resync_rotation`
- `reorder_show`
- `remove_show_item`
- `insert_spot` — **exception:** do the source resolution AND `self.precache.ensure(src)` BEFORE acquiring the lock (they can hit the NAS and must not block other operators). Acquire the lock only for: reading engine status → building/pushing the mutation → mutating `st["fed"]` → save.
- `fire_due_spots` (each inner `insert_spot` call re-enters the RLock — fine)
- `tick` — see 1d for the exact lock scope.
- `_resync_show` (called only from locked callers, but wrap anyway — RLock makes it free)

### 1c. Move the play_next endpoint's state surgery into the class

The `/api/engine/play_next` endpoint currently does `feeder._load_state` / `st["fed"].insert(0, ...)` / `feeder._save_state` inline. That is feeder state surgery outside the class. Create:

```python
def insert_manual(self, conn, path: str, title: str | None) -> tuple[bool, str]:
```

on `Feeder`, containing the endpoint's current logic (precache.ensure BEFORE the lock, same as insert_spot; then under `self._lock`: status → insert_next mutation with the existing 409-retry pattern → `st["fed"].insert(0, ...)` → save). Return `(ok, title_or_error)`. Rewrite the endpoint to call it and raise the same HTTPExceptions it does today (502 engine unreachable, 400 uncacheable, 502 on non-202).

### 1d. tick() lock scope

`tick()` acquires `self._lock` at the top (before `_load_state`) and releases it after `_save_state`, **but the eviction/lookahead work moves OUTSIDE the lock** (see Task 2). The batch-building NAS copies stay inside the lock — that is acceptable ONLY because Task 2 shrinks the fed batch to a few tracks and the lookahead pre-warms the cache, so `precache.ensure` at feed time is normally an instant hit. Do Task 1 and Task 2 together, in this order, in the same session.

### 1e. Read-only endpoints

`api_queue` and `api_rotation` read `feeder._load_state(conn)` without the lock. Wrap just the state-read (and any use of `st["fed"]`) in `with feeder._lock:` so they see a consistent snapshot. Keep the lock out of the response-building where possible; these must stay fast.

---

## Task 2 — Decouple feed depth from cache depth

**File:** `services/core/engine_bridge.py`, plus `config/config.example.json`.

### 2a. Config

- New config key `feed_ahead_tracks`, default **3**. In `Feeder.__init__`:
  ```python
  self.feed_ahead = max(1, int(cfg.get("feed_ahead_tracks", 3)))
  ```
- `precache_target_minutes` (existing, default 45) now governs the **disk cache lookahead only**. Keep `self.target_sec` but rename nothing else.
- Add `"feed_ahead_tracks": 3` to `config/config.example.json` with the other engine/core keys, and a one-line comment in the file if it has a comments convention (it's JSON — if no comment mechanism exists, document the key in `DEPLOY.md`'s config section instead).

### 2b. tick() feeds by track count, not seconds

Replace the two duration-based conditions in `tick()`:

- Top-up short-circuit: `if pending and pending_sec >= self.target_sec:` becomes `if pending and len(pending) >= self.feed_ahead:`
- Batch loop: `while pending_sec < self.target_sec and len(batch) < MAX_FEED_BATCH:` becomes `while len(pending) + len(batch) < self.feed_ahead and len(batch) < MAX_FEED_BATCH:`

`pending_sec` bookkeeping can stay (it's harmless) or be removed — your call, keep the diff minimal.

### 2c. Cache lookahead (the 45-minute disk buffer)

New method on `Feeder`:

```python
def _cache_lookahead(self, conn, st_snapshot: dict) -> set[str]:
    """Pre-copy upcoming rotation material to the precache dir WITHOUT
    consuming the cursor. Returns the set of cache paths to protect
    from eviction. Runs OUTSIDE self._lock — only touches the snapshot."""
```

Behavior:

- Input `st_snapshot` is a `json.loads(json.dumps(st))` deep copy taken while the lock was held (see 2d).
- Simulate forward from the snapshot's cursors, same precedence as `tick()`: active show first (if any, `wrap=False`), then base rotation (`wrap=True`). Use copies of the cursor dicts so nothing persists.
- For each resolved item:
  - Only **deterministic** items get cached ahead: `item_type == "file"` (a `folder-rotation`/`folder-random` item resolves differently at feed time — caching a guess wastes copies; skip them but still count their estimated duration).
  - Call `self.precache.ensure(src)`; on success add the returned cache path to the keep-set.
  - Accumulate duration via `self._duration_of(conn, src)` (fall back `DEFAULT_TRACK_SEC`).
- Stop when accumulated duration ≥ `self.target_sec` OR after `MAX_FEED_BATCH * 2` items (runaway guard on pathological playlists).
- Wrap the whole body in try/except with `log.exception` — a lookahead failure must never break the tick. Return whatever keep-set was accumulated.
- Note: it needs a DB conn for `_duration_of`/`pl.get_items`/`pl.resolve_item`. It is called from the feeder loop thread with that thread's own conn — safe. Do NOT call it from request threads.

### 2d. Restructure the tail of tick() and eviction

Current tail: `_save_state` → `_evict(conn, st, status)` (evict keeps only fed paths + now_playing).

New structure:

1. Inside the lock, just before releasing: `snapshot = json.loads(json.dumps(st))` and capture `keep_fed = {e["path"] for e in st["fed"]}` plus `now_playing`.
2. Release the lock.
3. `keep = keep_fed | {now_playing if any} | self._cache_lookahead(conn, snapshot)`
4. `self.precache.evict_except(keep)`

Apply the same pattern to the early-return "topped up" path (it currently also evicts). The other early returns ("no active playlist", "nothing to feed", push-failure rollback) should NOT evict — same as today.

### 2e. Eviction grace age (belt-and-suspenders)

**File:** same, class `Precache`.

- In `ensure()`, when writing the manifest record, add `"cached_at": time.time()`.
- In `evict_except()`, add parameter `min_age_sec: float = 600.0` and skip any victim whose `cached_at` is younger than that (missing `cached_at` on legacy records = treat as old, evictable). This makes the "operator cached a file between snapshot and evict" race harmless: a freshly cached file cannot be evicted for 10 minutes no matter what the keep-set says.

### 2f. Sanity note on behavior change

After this task, a P2 death leaves P1 with only ~3 queued tracks instead of ~45 min. That is BY DESIGN: when P1 drains it enters emergency mode, whose filler tier plays real music from the precache dir — which now still holds ~45 min of upcoming rotation material. Document this in the CHANGELOG entry so nobody "fixes" it later.

---

## Task 3 — Spot failure grace window

**File:** `services/core/engine_bridge.py`, `Feeder.fire_due_spots`. No schema changes.

Current: `spotmod.mark_fired(conn, rule, now)` runs whether or not `insert_spot` succeeded.

New behavior (in-memory, no migration):

- `Feeder.__init__`: `self._spot_retry: dict[int, float] = {}` (rule id → first-failure epoch).
- In `fire_due_spots`, per due rule:
  - On **success**: `mark_fired`, `self._spot_retry.pop(rule["id"], None)` — unchanged otherwise.
  - On **failure**: record `self._spot_retry.setdefault(rule["id"], now)`. If `now - self._spot_retry[rule["id"]] < 600` (10 min grace): do NOT `mark_fired` — the rule stays due and retries next tick. Log at `warning` once per minute at most (guard with the timestamps you already have — acceptable to just log every tick if simpler, ticks are 5s… no: throttle it. Keep a `_spot_last_warn: dict[int, float]` and warn at most every 60s per rule).
  - After the grace window expires: `mark_fired`, log `log.error("spot rule %d MISSED its window after 10min of retries: %s", ...)`, and pop the retry entry. The error level makes it findable in `data/logs` for affidavit reconciliation.
- Engine-unreachable / emergency-mode early return at the top of `fire_due_spots` stays exactly as is (rules simply stay due — that path already retries correctly).

---

## Task 4 — Fix stale docs in .claude/CLAUDE.md

**File:** `.claude/CLAUDE.md` (StudioFire project). Verify each claim against the repo before writing (read `start-all.bat`, `services/*/main.py`, `healthcheck.bat` args) — do not copy blindly:

- Quick Start: replace `python main.py` with the real entry points, e.g. `start-all.bat` for everything, or individually `python -m services.engine.main config/config.json` (P1), `python -m services.core.main config/config.json` (P2, GUI :8080), `python -m services.worker.main config/config.json` (P3). Confirm exact arg conventions from the files.
- Remove `python main.py --dry-run` from the workflow section (no such flag); replace with running the test suite (Task 5 command) as the local verification step.
- Key Files table: `main.py` row → `services/` row ("four isolated services: engine / core / worker / poller — see REVIEW.md §2").
- Tech Stack: replace `**Streaming:** KDPI protocol` with the real chain: `**Audio path:** sound card → Barix Instreamer → WireGuard VPN → transmitter (KDPI)`.
- Leave everything else (health-check emphasis, memory pointers, checklists) intact.

---

## Task 5 — Tests

### 5a. Establish how the suite runs

Inspect `tests/` first. If pytest is installed (`python -m pytest --version`), run:

```
python -m pytest tests -q -x --ignore=tests/torture.py --ignore=tests/test_supervisor_bench.py
```

(the torture/bench suites drive real mpv for long periods — skip them; they are a human-run gate). If pytest is absent, run each `tests/test_*.py` directly with python and check exit codes. Use the Anaconda interpreter the project uses: `"/c/Users/markd/anaconda3/python.exe"`. Record the before-state.

### 5b. New regression test — tests/test_feeder_concurrency.py

Model it on the existing fakes/fixtures in `tests/test_engine_bridge.py` (read that file first and reuse its stub-engine pattern rather than inventing one). The test:

1. Build a Feeder against a stub engine (accepts all mutations, tracks pending ids) and a real `Precache` in a temp dir, with a small library of tiny generated files as the "NAS".
2. Monkeypatch `Precache.ensure` to `time.sleep(0.05)` before delegating to the real implementation (simulates slow SMB).
3. Thread A: call `feeder.tick(conn_a)` in a loop ~20 times.
4. Main thread, concurrently: call `feeder.insert_spot(...)` and/or `feeder.insert_manual(...)` ~20 times (own connection; SQLite is WAL — use separate connections per thread exactly like production does).
5. After joining, assert for EVERY inserted id: (a) the id is present in `feeder_state`'s `fed` list, and (b) its cached file still exists on disk (was not evicted).
6. Also assert `evict_except` honored the grace age: cache a file, call `evict_except(set())` immediately, file must survive; call with `min_age_sec=0`, file must be gone.

Sanity check the test catches the bug: temporarily comment out the `with self._lock:` in `tick` (or run the test against the pre-fix code first) and confirm it FAILS, then restore and confirm it passes. Note the confirmation in `REVIEW.md`.

### 5c. Full suite green

All suites from 5a green after the changes. `tests/test_engine_bridge.py` will likely need updates where it asserts duration-based top-up (Task 2b changed the condition) — update assertions to the track-count model, do not weaken unrelated assertions.

---

## Task 6 — Wrap up

1. `CHANGELOG.md`: add an Unreleased section entry covering: feeder state serialization (RLock), feed/cache depth decoupling (`feed_ahead_tracks`, default 3; precache still 45 min + it feeds P1's emergency filler tier by design), eviction grace age, spot retry grace window, doc corrections.
2. `REVIEW.md`: append a dated section "Feeder hardening (ticket: StudioFire feeder hardening)" — what changed, test evidence (including the 5b fail-then-pass confirmation), and this explicit reminder: **the 72-hour torture gate (`tests/torture.py` + `scripts/soak_monitor.py`) must be re-run on the bench with operator-action chaos before this branch reaches the on-air PC. That re-run is a human decision, not part of this task.**
3. Commit(s) on `feeder-hardening` with clear messages. NO push, NO deploy, NO service restarts.
4. Final response should list: files changed, test results (counts), anything you deviated on and why, and anything you found that this spec missed.

## Explicitly OUT of scope

- Anything in `services/engine/` (P1).
- The P4 monitor poller.
- Running the 72h torture/soak gate.
- Deploying, pushing to GitHub, restarting services, touching `config/config.json`.
- The `MUSIC LIST *.lst` files in the repo root — leave them exactly where they are (playlists may reference them by path).
