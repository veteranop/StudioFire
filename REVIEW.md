---
tags: [reference, studiofire]
status: active
created: 2026-07-15
up: "[[PROJECTS-INDEX]]"
---


[[01-Active-Revenue]]

# StudioFire — Architecture & Status Review (for external review)

*Snapshot for a second-opinion review. Self-contained: you don't have the code.
Please critique the architecture, the risky subsystems, and the tuning list at
the end — especially the feeder state machine and the "never silent" guarantee.*

---

## 1. What it is
A from-scratch **radio automation platform** (a ZaraRadio replacement) for a
small FM station. Windows, Python (Anaconda). It plays music/ads/IDs 24/7 to a
Barix encoder → VPN → transmitter. Non-technical DJs use a **web GUI** to build
playlists and run the board. Guiding law from the owner: **"audio on is
everything"** — dead air is the cardinal sin.

Scale: ~4 TB library on a Synology NAS (SMB), ~40k tracks, mixed mp3/m4a/wav.

## 2. Architecture — 4 isolated processes
Deliberately segmented so nothing can take down playback:

- **P1 — Audio engine** (`services/engine`, ~1.4k LOC, **stdlib only**). A thin
  supervisor around **mpv** (JSON IPC over a Windows named pipe). Owns a
  persisted queue (atomic JSON file). Localhost HTTP control surface on :7701
  (`/status`, `/queue`, `/op`). **The only process in the audio path; never
  touches the database or the network beyond localhost.**
- **P2 — Core/GUI** (`services/core`, ~2.6k LOC). FastAPI web app + the
  **feeder** (feeds P1 from the library), SQLite (WAL). Talks to P1 over
  localhost HTTP. This is where all the complexity lives.
- **P3 — Indexer** (`services/worker`, ~230 LOC). Incremental NAS scan, reads
  tags with mutagen into SQLite. Below-normal priority.
- **P4 — Monitor** (planned, not built): poll transmitter/Barix/UniFi.

P1↔P2 use a **queue_version protocol**: every queue mutation carries a
monotonic version; P1 rejects stale versions (409) and P2 re-syncs. Only P2
writes SQLite; P1 is DB-agnostic so it can run even if P2/DB/NAS are all down.

### The "never silent" guarantee (P1)
- **3-tier failover** on any "can't start next track": pre-cached queue →
  emergency-folder loop → a baked-in ffmpeg tone (`av://lavfi`) that exists even
  if every file on disk is gone.
- **1 s watchdog**: mpv liveness (ping) + **position-advancing** check (catches
  silent hangs where mpv is "alive" but frozen) → restarts mpv.
- Queue state, emergency_mode, and a JSONL **play journal** (as-aired truth) are
  all persisted with atomic writes + fsync; a crash mid-write can't corrupt them.
- Torture-test exit gate (design intent): **zero silence > 2 s over 72 h.**

### Pre-cache (P2 feeder)
P1 only ever plays **local files**. P2 copies each upcoming NAS file to a local
cache (temp → size-verify → atomic rename; a manifest tracks valid entries),
keeping **~45 minutes** of audio queued ahead, so a NAS/network blip never
starves playout. Files are evicted after airplay.

## 3. Current state — built & working
Phases 0–2 largely done; parts of 3.

- **Engine core**: failover chain, watchdog, restart, journal. Real-mpv bench
  covers 8 torture scenarios (exhaustion→filler, kill mpv→recover, forced
  emergency survives restart, etc.).
- **Web GUI**: login + roles (admin/operator), On Air dashboard, Playlists
  editor, Settings (station folders w/ folder browser), Reports, backup/restore.
- **Library**: recursive incremental indexer; full-text-ish search.
- **On Air controls**: GO/STOP On Air (master pause/resume), Skip, **Stop after
  current song** (finishes the song then holds the next at 0:00). Automatic
  emergency filler still triggers on its own.
- **Rotation list** (center of On Air): shows the **whole playlist that's
  actually airing** (base rotation, or a show while one is on), with the on-air
  song **pinned to the top** and already-played songs hidden; searchable;
  **drag-reorder / remove save to the playlist and re-sync the live buffer
  immediately**; read-only while a show is on air.
- **Global library search → Insert Next**: drop any song into the live queue as
  a one-off (doesn't edit the playlist).
- **Spots** (IDs/ads/jingles/PSAs): per-folder rules, 4 triggers (every N min /
  clock minutes / one-off datetime / manual), round-robin selection, inserted at
  the next song boundary, fire during shows too. Live countdowns.
- **Scheduling / shows**: a scheduled *playlist* plays once through then returns
  to the rotation. A due scheduled show **takes over whatever is airing** (incl.
  another show) at its time; per-entry **Start now (hard cut) / Cue next (after
  this song) / Remove**; a **Stop show** button; one show at a time.
- **Now Playing / Log metadata**: artist/album/song shown, read from tags in the
  **local cached copy** (index- and path-independent). As-aired History rail +
  Reports (CSV export) from the play journal.
- **Ops**: `start-all` / `stop-all` / `restart-all` scripts, a config-gated GUI
  "restart everything" button, detached launcher, `DEPLOY.md`.

### Robustness fixes made recently
- SQLite `check_same_thread=False` (FastAPI threadpool moved a request's
  connection across threads → intermittent 500s on every DB endpoint).
- Pre-cache **manifest write race** (WinError 32): serialized with a lock +
  retry on `os.replace`.
- Indexer only flags a track *missing* when its **folder was actually readable**
  this pass — a slow/hidden NAS folder can no longer wipe the library from search.
- **Auto-relink**: many playlists (pre-Synology) point at dead paths; a one-click
  pass repoints each out-of-root item to the same-named indexed file.

### Tests
~**272 assertions** across 10 suites, incl. two that drive **real mpv**
(supervisor bench + a full P2↔P1 end-to-end that exercises the feeder, shows,
spots, rotation edits, take-over, stop-show, metadata). All green.

## 4. The part that most needs review — the feeder (P2)
One `tick()` (every 5 s) now does a lot, and I want a hard look at whether the
state machine is sound and whether it's too complex:

- Reconciles its bookkeeping against P1's `pending_ids` (keeps the currently-
  playing entry too, to map the play-head → playlist item for the "now" marker).
- **Finalizes** a finished show only once its tracks have *aired* (not merely
  been fed), so a short pre-fed show doesn't clear early.
- **Fires** a due scheduled show — **interrupting a running show** if needed
  (`_finish_show` then `_start_show` → `clear_pending` so the current song ends,
  then the show begins).
- Feeds the active program (show once-through, else base rotation forever),
  reading tags from each cached file, tracking `pl_item_id`/`prog`/metadata per
  entry.
- **Injects spots** (`insert_next`) on due rules.
- **`resync_rotation`** (after a playlist edit): recompute the cursor to just
  after the play-head, `clear_pending`, re-feed — so edits take effect on air
  immediately without dead air (current song keeps playing).
- All mutations go through the queue_version protocol with 409 re-sync.

**Concern:** shows + spots + resync + metadata + reconciliation are interleaved
in one component. Is this the right decomposition? Are there orderings (e.g. a
spot firing during a show take-over during a resync) that could drop a track or
briefly starve P1? The failover keeps it from going *silent*, but I care about
correctness, not just "not silent."

## 5. Known issues / tuning list (please weigh in)
1. **Buffer latency vs. responsiveness.** The 45-min pre-cache means the
   now-marker, metadata, and even edits only reach the *play-head* after the
   buffered songs drain — unless a resync/edit rebuilds the buffer. Is 45 min
   too much? Should the marker/metadata be derived differently so the UI reflects
   reality faster without shrinking the safety buffer?
2. **Metadata only on freshly-fed tracks.** Reading tags happens at feed time;
   entries already buffered before a deploy show old titles until they cycle.
   Acceptable? Or backfill?
3. **Torture gate not re-run** after all the feeder changes. The "zero silence
   over 72 h" bench predates shows/spots/resync/metadata. Highest-priority thing
   to re-validate before go-live?
4. **Path/index model.** Playlists should store portable UNC paths like
   `\\KDPI-Media\music\...` and the index should walk the local NAS root set in
   `paths.nas_music_root` (`//KDPI-Media/music/G`). Legacy `Z:` playlists are
   still supported for playback compatibility, but new deployments should avoid
   mapped-drive paths where possible.
5. **Legal top-of-hour ID** is currently "a spot rule with a clock trigger,"
   boundary-aware (so it airs a few min *after* :00). Good enough for FCC, or
   does it need a hard guarantee/window?
6. **Show take-over timing.** Scheduled shows join at the next song boundary
   (finish current song). Some DJ shows may need exact-time hard cuts. Worth a
   per-show option?
7. **Deployment shape.** P1+P2 must co-reside (localhost:7701), so one PC runs
   everything and DJs browse in. Right call, or should the web/DB be separable
   from the on-air engine? NSSM for auto-restart is documented but not scripted.
8. **No P4 monitor yet** (transmitter/Barix/UniFi). Crossfade (Phase 3) not done
   — butt-cut only, which the owner accepted for MVP.
9. **GUI "restart everything" button** relies on relaunching in an interactive
   session (console windows); it's gated off for the on-air PC. Fine, or should
   restart be NSSM-driven only?

## 6. Specific questions
- Is the **feeder** over-scoped? If you'd split it, where are the seams?
- Any **ordering/concurrency** hole left after the SQLite + manifest fixes?
- Does **"scheduled show always takes over"** risk a gap or a thrash loop with
  back-to-back schedules?
- Is **tags-from-cache** a smell that will bite later, or pragmatic?
- What would *you* re-test before this goes on a live transmitter?

## 7. Running it (context)
`python -m services.engine.main config/config.json` (P1), same for
`services.core.main` (P2, GUI on :8080) and `services.worker.main` (P3). Web app
→ DJs browse to `http://<box>:8080`. Full history in `CHANGELOG.md`; binding
spec in `PLAN.md §10`; deploy steps in `DEPLOY.md`.

## 8. Feeder hardening — 2026-09-23 (TimeTrax #634, branch `feeder-hardening`)

Follow-up on §4/§6 above. Architecture review (Claude Fable 5) found the
feeder's `tick()` was a genuine lost-update race, not just a "too complex"
concern: `feeder_state` (SQLite settings) was read-modify-written with no
lock, and the background feeder loop held its in-memory copy across slow NAS
copies inside `tick()` while FastAPI request threads (`insert_spot`,
`/api/engine/play_next`, show/rotation edits) concurrently loaded-mutated-
saved the same state. The stale save could win, silently dropping an
operator's cued track/spot from `st["fed"]`, after which `_evict()` would
delete its just-cached file while it was still pending in P1's queue — P1
skips it at prefetch ("unplayable at prefetch") and it never airs, with no
error surfaced anywhere. Execution spec: `.claude/upgrade-instructions.md`
(run by Claude Sonnet).

**Changes (`services/core/engine_bridge.py` unless noted):**
- `Feeder._lock` (RLock): every method that reads-modifies-writes
  `feeder_state` now holds it for the full critical section — `activate`,
  `tick`, `insert_spot`, the new `insert_manual`, `fire_due_spots` (via
  `insert_spot`), `stop_show`, `start_show_now`, `resync_rotation`,
  `reorder_show`, `remove_show_item`, `_resync_show`. `insert_spot`/
  `insert_manual` do their NAS resolve+copy *before* taking the lock (a slow
  copy must never block another operator or the feeder loop) and only hold
  it for the queue push + bookkeeping. Read-only endpoints (`/api/queue`,
  `/api/rotation`) take the lock too, for a consistent snapshot.
- Moved `/api/engine/play_next`'s inline state surgery into
  `Feeder.insert_manual` — no more feeder-state mutation living outside the
  class.
- **Feed depth decoupled from cache depth.** `tick()` now feeds P1's queue
  only `feed_ahead_tracks` tracks deep (config `core.feed_ahead_tracks`,
  default 3) instead of `precache_target_minutes` (45 min) worth. The
  45-minute *disk* cache is unchanged and still the real NAS-outage
  protection — a new `_cache_lookahead()` keeps it warm from a lock-free
  snapshot, run *after* releasing `self._lock` (NAS I/O must never happen
  inside it). This is intentional: if P2 dies, P1 now drains to only a few
  queued tracks, enters emergency mode, and its filler tier plays real
  rotation music straight from the still-full precache dir — don't "fix"
  this back to a deep P1 queue.
- `Precache.evict_except()` gained `min_age_sec` (default 600s): a file
  cached more recently always survives eviction regardless of the keep set —
  closes the residual window between a feeder snapshot and the eviction call
  itself.
- `fire_due_spots`: a failed spot insert no longer immediately calls
  `mark_fired` (which would burn the rule's only chance to fire). It retries
  for up to 10 minutes (throttled warning log), then finally marks fired
  with a `log.error` so a genuinely missed ad/legal-ID is findable in the
  affidavit trail.
- `.claude/CLAUDE.md`: corrected the `python main.py` / `--dry-run` /
  "KDPI protocol" inaccuracies flagged in the original spec.

**Tests:**
- New `tests/test_feeder_concurrency.py` (stub P1, no real mpv — isolates
  Feeder's own locking): two threads hammer `tick()` and `insert_spot`/
  `insert_manual` concurrently against a `Precache.ensure` monkeypatched to
  sleep (simulated slow SMB), then assert every successful insert survived
  in `feeder_state` and its cache file wasn't evicted while still queued.
  **Confirmed it actually catches the bug**: with `tick()`'s
  `with self._lock:` temporarily replaced by `contextlib.nullcontext()`
  (and `feed_ahead_tracks`/`MAX_FEED_BATCH` tuned so `tick()` never takes
  the fast "topped up" shortcut, to keep its unprotected critical section
  wide enough to collide), the test failed reliably across 3 runs
  (`FAIL: every inserted entry is still in feeder_state`); reverting the
  lock passed reliably across 3 more runs. Also covers the eviction grace
  age directly.
- `tests/test_engine_bridge.py`: updated the one assertion that depended on
  old eviction-on-first-call behavior (now needs `min_age_sec=0` to force
  immediate eviction — added a companion assertion that a fresh file
  survives the default grace window). No other existing assertions needed
  changes — the duration-based top-up condition was already migrated to the
  track-count model as part of this change without breaking any assertion,
  since the tests check `queue_len >=`/wrap behavior, not raw durations.
- Full suite green: `test_control` (13), `test_core_foundation` (23),
  `test_indexer` (24), `test_journal` (9), `test_playlists` (67),
  `test_queue_store` (23), `test_schedule` (30), `test_spots` (37),
  `test_gui_smoke` (59), `test_feeder_concurrency` (9, new),
  `test_engine_bridge` (109, real mpv) — 403 checks total, 0 failures.
  `torture.py` / `test_supervisor_bench.py` intentionally not run (human-run
  bench gate, out of scope here).

**Deviation from the execution spec:** the spec assumed a "stub-engine
pattern" already existed in `tests/test_engine_bridge.py` to model the new
test on. It doesn't — that suite drives a real P1 (mpv + `ControlServer`)
end-to-end. Wrote a small thread-safe `StubEngine` from scratch in the new
test file instead (accepts mutations, tracks pending ids, enforces the
`queue_version` protocol) so the concurrency test doesn't pay real-mpv
startup cost and isolates exactly the thing under test — Feeder's locking,
not P1's.

**Not done here (explicitly out of scope per the spec):**
- Nothing in `services/engine/` (P1) touched.
- P4 monitor poller untouched.
- **The 72-hour torture/soak gate has NOT been re-run.** It predates
  shows/spots/resync/metadata even before this change, and this change adds
  new locking behavior on top. This is a human decision, not something to
  run unattended — **re-run it on the bench, with operator-action chaos
  (hammering Insert Next / spot play-now / reorder while the feeder is
  mid-fill on a slow/throttled NAS path) added to the existing matrix,
  before this branch goes anywhere near the on-air PC.**
- No deploy, no push, no service restarts, `config/config.json` untouched.

## 9. John (KDPI) feedback batch — 2026-09-25 (TimeTrax #644, branch `john-feedback-batch`)

Branched off `feeder-hardening` so both ship together. John's nine requests
after the first live show, plus two latent bugs found along the way.

**P1 engine — song fades (`services/engine/supervisor.py`, `main.py`).**
This is the only P1 change, and the one to review hardest.
- Approach: a new `engine-fader` thread ramps mpv's `volume` property every
  100ms. It does NOT use per-file `af`/`afade` filters: those depend on the
  mpv version's `loadfile` syntax, rebuild the filter graph, and need the
  duration up front. Pure `fade_volume(pos, dur, fade_in, fade_out)` is
  unit-tested.
- It is sequential fade-out → fade-in, not an overlapping crossfade. One
  mpv instance can't overlap two files, and a second instance on the live
  path is not a risk worth taking. It matches what John asked for ("fade out
  and new song fade in").
- Which files fade: `FADE_SOURCES = {"playlist", "show", "manual"}`. Spots
  never fade, and emergency/baked-in filler never fades.
- Safety design (the fader must never cause quiet/dead air):
  - The owner thread sets each file's STARTING volume synchronously at
    start-file (0 for a fading song, 100 for everything else), so a spot
    can't inherit a faded-out volume even for one tick.
  - `_fade_gen` bumps on every track change, and the fader discards a
    reading taken across a change.
  - Unknown pos/duration → full volume. The fader never guesses toward
    silence.
  - Every 5s the fader re-reads mpv's real volume and re-asserts it on
    drift (e.g. after an mpv restart, which also resets `_fade_sent`).
  - Any unexpected fader exception → send full volume.
  - Fades off (both 0) → no fader thread, and start-file never touches
    volume: exactly the old behavior.
- Defaults: `engine.fade_out_sec` 4, `engine.fade_in_sec` 1.5, set in
  `main.py`. The supervisor's own default is off, so the existing bench runs
  un-faded.
- Known limits:
  - Operator Skip cuts without a fade-out.
  - With gapless audio, the last ~0.2s of a spot that is followed by a song
    may be cut to the song's fade-in start (0). Spots normally end on
    silence.
  - A VBR file with a wrong duration estimate fades early or late.

**P2 changes.**
- Migration 11: `playlist_items.duration_sec`.
  - `parse_lst` now keeps Zara's per-line ms. It also treats a negative or
    garbage duration (a real `-2` was in JB Playlist 4) as unknown; before,
    that swallowed the whole line into the path.
  - Stored on import, duplicate, and add. The add API probes the length:
    index first, else a mutagen header read, for single adds only.
  - The feeder backfills it from the cached copy's real length the first
    time a song is fed (file items only, never folder items).
- `export_lst_text` writes the item's own duration. Before, it used
  index-only durations, which blanked every duration outside the music root
  on any GUI save.
- `write_lst` falls back to UTF-8 + BOM when a path isn't cp1252-encodable.
  Before, `errors="replace"` turned `√` into `?` and silently broke that
  path forever. Plain playlists stay cp1252 for Zara.
- `/api/rotation` items carry `duration`, plus a `timing` block: count,
  total, time left after the on-air song, and unknown-length counts. It
  drives the Now Playing card's playlist line.
- New endpoint: `GET /api/playlists/{pid}/stats`.
- `schedule.next_occurrence()` / `next_label()`: `list_waiting` now sorts by
  the real next airing, interleaving one-time and recurring shows (it used to
  sort all one-shots first). Each row gets `next_at`/`next_label`. Only the
  UI uses this order; firing still goes through `due()` and is unchanged.
- `asset_v` cache-buster on `style.css`. Found during the visual check: a
  stale cached stylesheet would have hidden every layout fix after deploy.

**UI.**
- Spot search box.
- Date beside the clock. Below 1600px the clock moves into the bar's normal
  flow; it used to overlap the nav at 1366.
- Schedule/spot lists wrap, and their buttons drop under the text.
- The log rail moves below the main area at ≤1440px.
- Calendar cells use `minmax(0,1fr)` and names wrap.
- Playlist page: stats line, per-song lengths, and a file browser card
  (+ Add / ▶ Next / Add all, remembers the last folder). Adds no longer
  reload the page.

**Tests.** The full suite is green: 484 checks across 13 suites, 0 failures.
- New: `tests/test_fader.py` (19 checks). It samples real mpv's volume
  while playing song → spot → manual song → filler, and asserts:
  - the song fades in and out;
  - the spot is at 100 in every sample from its first instant;
  - filler is at 100.
  Ran 4× with no flakes.
- `test_playlists` +15, `test_schedule` +8, `test_engine_bridge` +4.
- `test_supervisor_bench` (35) also re-run and green.
- Visual check: the real P2 pages were rendered at 1366×768 and 1920×1080
  against a temp DB copy with a fake engine. The file-browser add was
  verified end to end, including the `.lst` save (duration + cp1252).

**Not done / gates:**
- The torture/soak gate from §8 still applies, and now also covers the
  fader. Bench-listen to the fades before anything goes on the on-air PC.
- No deploy, no push, `config/config.json` untouched. Fades turn on by
  default once the new `main.py` runs; to disable, set
  `engine.fade_out_sec`/`fade_in_sec` to 0 in the station config.
- The on-air PC is on the stress-tested build: neither this branch nor
  `feeder-hardening` is live.

## 10. Self-update from GitHub Releases — 2026-09-25 (TimeTrax #644, same branch)

Goal: GitHub is the only source; any deployed station can pull a patch.

**Design.**
- **Channel:** published GitHub Releases on the public repo (no tokens on
  boxes). A box downloads the tag's `zipball`.
- **Release process:** `scripts/release.py` is the one way to publish.
  It keeps `VERSION` == tag, uses the `[Unreleased]` changelog section as
  the release notes, and runs the whole test suite first.
- **Updater:** `services/updater.py`, stdlib-only. The steps, in order:
  1. Verify before touching anything: VERSION == tag, required files
     present, every `.py` compiles, `requirements.txt` unchanged.
  2. Back up the code and the DB (sqlite online backup).
  3. Mirror-install `MANAGED_*`. This must stay in sync with
     `installer/StudioFire.iss [Files]`.
  4. Restart only what changed (engine only for `services/engine/**`).
  5. Health-check each service. P2 `/health` now reports the version it
     was started with.
  6. Auto-rollback on any failure, including unexpected exceptions and a
     half-finished copy.
- **GUI install:** spawns the helper through WMI (`Win32_Process.Create`),
  so it lives outside P2's process tree and any NSSM job. P2 is one of the
  things it restarts. NSSM boxes: kill the python process and NSSM revives
  it with the new code. Plain boxes: relaunch detached, like
  `restart_all.py`.
- **Checking vs installing:** P2 checks every 6h (`core.update_check`,
  default on via `load_config`, off in bare test apps). Install is admin
  only, and nothing ever auto-installs on a live station.

**Tested.**
- `tests/test_updater.py` (27 checks), run against a fake GitHub with
  faked restarts. It covers: a good update, a web-only change leaving the
  engine alone, mirror-deletes, config/DB untouched, the backup contents,
  4 pre-install rejections, rollback, a double failure flagged "call
  support", the lock, git refusal, and `--tag --force`.
- **Real end-to-end run (not in the suite, needs free ports and no running
  StudioFire):** a throwaway station on 8094/7794 (mpv `--ao=null`) plus a
  fake GitHub. The update was triggered through `/api/update/install`, so
  the helper really was created via WMI.
  - Update 1.1.0 → 1.2.0 (engine + web): about 6s end to end, all three
    services came back with new PIDs on the new version.
  - A release whose `app.py` crashes on import was detected as "crashed on
    startup: core" about 14s after restart and rolled back to 1.2.0, with
    the engine playing throughout.
- Found and fixed along the way:
  - The cp1252 console crashed the updater on printing "→", *after*
    installing, and unexpected exceptions skipped rollback.
  - `installed`/`restarted` were set after the step, so a half-done copy
    or restart wouldn't have rolled back.
  - Backup folder names were second-resolution, so a retry in the same
    second failed. Found by a flaky full-suite run and now covered by a
    regression check.

**Gaps / decisions for Mark.**
- **Existing 1.0.x boxes need one manual installer upgrade** to get the
  updater (bootstrapping).
- **Install-folder permission:** the service account must be able to
  modify the install folder. New installers grant `users-modify` on
  `{app}`; old installs need it granted once.
- **New Python packages → full installer,** by design.
- **`v1.0.1` on GitHub has VERSION 1.0.0 inside,** so stations refuse it
  (correctly). The next scripted release supersedes it.
- **NSSM crash-loop:** in NSSM mode a crash-looping service keeps
  reappearing, so fail-fast doesn't trigger and rollback waits the full
  120s timeout. The web GUI is down during that window; audio is
  unaffected unless the engine itself is the thing crashing.
- **NSSM path not exercised live here** (no NSSM on the dev box). The logic
  is "kill → NSSM revives", which is what `AppExit Restart` does. Verify on
  the bench PC with services installed before the first real release.

## 11. Two-deck engine: true crossfade — 2026-09-25 (TimeTrax #644)

The §9 fader (a fade-out, then a fade-in, on ONE mpv) produced exactly the
dip Mark heard on the dev PC ("fades out, stops, then silently fades in").
John asked for a real crossfade, which needs two players sounding at once.
Mark chose the two-deck design over a bolt-on "tail helper" player.

**Design (`services/engine/supervisor.py`, rewritten).**
- **Decks:** two mpv players (pipes `<pipe>-a` / `<pipe>-b`). Each item
  plays start to finish on one deck, and the decks alternate. Deck roles
  are `idle`, `preloaded`, `onair` and `fading`. The next queue item is
  loaded PAUSED on the free deck; this replaces mpv's playlist prefetch
  and gapless playback.
- **Mixer thread** (100ms): ramps the volume of fading decks based on
  position (`fade_volume`, so ramps survive pause), and posts `xfade_due`
  when an on-air song reaches `crossfade_sec` from its end. It only ever
  touches volume, never what's playing.
- **Crossfade rules:**
  - song → song: the new song rises from 0 while the old one falls, overlapping.
  - song → spot: the spot starts at 100 under the fade.
  - spot → anything: the next item starts at end-of-file at 100.
  - No crossfade for: files shorter than 2×crossfade, stop-after armed,
    paused, or emergency.
  - `crossfade_sec` 0 = back to back.
- **Owner thread is still the single writer.** "Now playing" bookkeeping
  (journal `track_start`, `current_index`, status) now happens when WE put
  a deck on air, not on mpv's start-file. Because of that, P2 sees the next
  song as now playing from the moment the crossfade starts.
- **Stale-event safety:** events carry the client identity AND mpv's
  `playlist_entry_id`. An end-file from a replaced player or an earlier file
  on the same deck is ignored. Only `eof`/`error` count; `stop` is ours.
- **Watchdog:**
  - Both decks: liveness → `deck_dead` → restart that deck. If it was on
    air, kick to the next item, same as before.
  - On-air deck: stall detection.
  - New: nothing on air while not paused for 2 ticks → kick (replaces
    mpv's `idle` event safety net).
- **Unchanged:** P2 protocol, queue/version protocol, journal events,
  status fields, control API, and the failover tiers. `sup._client` is kept
  as the on-air deck's client for test harnesses.
- **Config:** `engine.crossfade_sec`, default 4; an old `fade_out_sec` is
  honoured as the length. `fade_in_sec` is gone.
- **Audio device:** both decks open the same output, which is fine in
  WASAPI shared mode. **Exclusive mode would break this** (the second deck
  couldn't open the device). No station config sets it today.

**Tests.**
- `tests/test_fader.py` was rewritten (30 checks, real mpv, both decks
  sampled). It covers:
  - song→song overlap, old falling and new rising;
  - no dip: the louder deck never drops below 45%;
  - song→spot: the spot at 100 throughout, starting under the fade;
  - spot→song: waits, then starts at 100;
  - filler at 100;
  - `crossfade_sec` 0: no overlap, everything at 100;
  - skip mid-crossfade is a hard cut;
  - stop-after: no crossfade, and the next song is held.
- Green 6× in a row after replacing single-sample end checks with
  first-third vs last-third averages (sampler reads can drop under load).
- The full suite is green, including the unchanged
  `test_supervisor_bench` (35: failover, killed mpv, restarts, forced
  emergency), `test_engine_bridge` (113, real engine end-to-end) and
  `test_stale_mpv`.

- **`tests/torture.py` matrix (T1–T5, dead-air gate 2.0s):** 5 of 5 passes.
  Longest silence was 1.04–1.78s, down from 1.53s before these fixes. It
  found two real problems, both fixed:
  - `mpv_alive` only became true on the watchdog's first tick. The
    two-deck engine goes on air faster than that, so status briefly
    reported dead players. It's now set once both decks have started.
  - **A command sent to a just-killed mpv waited the full 2s IPC timeout,
    and that was 3.09s of dead air** in the restart storm. The old engine
    had the same latent bug. `MpvClient.command` now polls the process
    every 100ms and fails in about 0.1s. The owner's MpvDead handler now
    restarts the deck that actually died, not "the on-air one".

**Gate:** this rewrites the transmitter path. Run the 72h
`torture.py soak` on the bench PC before the on-air PC, and do listening
tests of song→song, song→spot, spot→song and skip mid-crossfade on the
real sound card (WASAPI shared mode).

## Related
- [[PROJECTS-INDEX]]
