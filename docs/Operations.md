---
tags: [reference, studiofire]
status: active
created: 2026-07-15
up: "[[PROJECTS-INDEX]]"
---


[[01-Active-Revenue]]

# Operations

Running, restarting, deploying, and soak-testing. Deploy details in [[StudioFire/DEPLOY|DEPLOY]].

## Dev environment
- Python = **Anaconda 3.13** (`C:\Users\markd\anaconda3\python.exe`) — mandate.
  Bare `python` on PATH is the Windows Store stub (lacks our deps).
- `mpv.exe` in `bin/` (gitignored; the installer fetches it).
- GitHub is the release/update channel; deploy also mirrors to
  `\\KDPI-Media\music\StudioFire`.

## Run / stop the stack
- `start-all.bat` / `stop-all.bat` — dev box (each service in its own window).
  On the on-air PC use auto-restarting Windows services (NSSM) — see [[StudioFire/DEPLOY|DEPLOY]].
- Detached (survives the shell): `python scripts/launch_detached.py [engine|core|worker]`
  — no arg launches all three; a name launches just that one.

## Restarting
- **GUI restart button** → `scripts/restart_all.py` (detached, console-free).
  It kills core/worker WITHOUT `/T` (so the helper, a child of core, survives)
  and tree-kills the engine (to also stop mpv). Logs to `logs/restart.log`.
  A `.bat`'s `start cmd /k` can't create windows from P2 — that's why. See
  [[StudioFire/docs/Gotchas|Gotchas]].
- P1 restart = a brief filler blip (crash-safe by design).

## Database
- SQLite WAL. Versioned migrations in `services/core/db.py` run at startup
  (schema_version is tracked in-DB, so deployed boxes auto-migrate). Current
  `SCHEMA_VERSION` = **9**.
- **Duplicate cleanup** (Settings → Duplicate cleanup) finds repeated songs and
  **moves** the extras into a `_Duplicates` folder in the music library (with a
  manifest; nothing is deleted; in-use files are protected).

## Updating a station
- GitHub Releases are the only update channel. In the GUI: **Settings → Software
  updates** (admin only). From the box: `update.bat` (`update.bat check` reports
  only). The updater verifies the release, backs up the code **and the DB**,
  swaps in the new code, restarts only what changed (engine only when engine code
  changed — the one restart listeners hear), health-checks, and auto-rolls-back
  on any failure. Full flow + the branch/release model:
  [[StudioFire/DEPLOY|DEPLOY]] and [[StudioFire/docs/Release-and-Branches|Release & Branches]].

## Soak test (the §10.7 gate)
- **Pass:** zero audible silence > 2 s over 72 h, and **zero phantom skips**.
- Live monitor: `python scripts/soak_monitor.py [hours]` — read-only, polls the
  running engine + tails the journal, writing `logs/soak_report.jsonl` every
  5 min. Use this to soak the real system under real use.
- Synthetic fault-injection: `python tests/torture.py soak 72` (its own engine).

## Tests
Run a module with `python -m tests.<name>` (20 `test_*` suites in `tests/`).
`scripts/release.py` runs the whole suite before publishing. Key ones:
`test_supervisor_bench` (real mpv, fault matrix), `test_engine_bridge` (P1+feeder
e2e, real mpv), `test_fader` (crossfade/volume, real mpv), `test_updater`
(self-update + rollback, faked GitHub), `test_feeder_concurrency`,
`test_spot_airing`, `test_gui_smoke`, `test_schedule`, `test_spots`,
`test_playlists`, `test_queue_store`, `test_indexer`, `test_duplicates`,
`test_time_format`. `test_supervisor_bench` / `torture.py` are the human-run
bench gate (not run unattended).

## Related
- [[StudioFire/docs/Release-and-Branches|Release & Branches]]
- [[PROJECTS-INDEX]]
