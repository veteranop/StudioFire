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
4. **Path/index model.** Playlists store `Z:\...` paths; the index walks `Z:/G`;
   pre-Synology playlists point elsewhere. I side-stepped metadata path-matching
   by reading tags from cached files, and added relink for playback. Is that the
   right call, or should paths be normalized/canonicalized in the DB?
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
