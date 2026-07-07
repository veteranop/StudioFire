# Operations

Running, restarting, deploying, and soak-testing. Deploy details in [[DEPLOY]].

## Dev environment
- Python = **Anaconda 3.13** (`C:\Users\markd\anaconda3\python.exe`) — mandate.
  Bare `python` on PATH is the Windows Store stub (lacks our deps).
- `mpv.exe` in `bin/` (gitignored; the installer fetches it).
- GitHub is the release/update channel; deploy also mirrors to
  `\\KDPI-Media\music\StudioFire`.

## Run / stop the stack
- `start-all.bat` / `stop-all.bat` — dev box (each service in its own window).
  On the on-air PC use auto-restarting Windows services (NSSM) — see [[DEPLOY]].
- Detached (survives the shell): `python scripts/launch_detached.py [engine|core|worker]`
  — no arg launches all three; a name launches just that one.

## Restarting
- **GUI restart button** → `scripts/restart_all.py` (detached, console-free).
  It kills core/worker WITHOUT `/T` (so the helper, a child of core, survives)
  and tree-kills the engine (to also stop mpv). Logs to `logs/restart.log`.
  A `.bat`'s `start cmd /k` can't create windows from P2 — that's why. See
  [[Gotchas]].
- P1 restart = a brief filler blip (crash-safe by design).

## Database
- SQLite WAL. Versioned migrations in `services/core/db.py` run at startup
  (schema_version is tracked in-DB, so deployed boxes auto-migrate).

## Soak test (the §10.7 gate)
- **Pass:** zero audible silence > 2 s over 72 h, and **zero phantom skips**.
- Live monitor: `python scripts/soak_monitor.py [hours]` — read-only, polls the
  running engine + tails the journal, writing `logs/soak_report.jsonl` every
  5 min. Use this to soak the real system under real use.
- Synthetic fault-injection: `python tests/torture.py soak 72` (its own engine).

## Tests
`python tests/<name>.py`. Key ones: `test_supervisor_bench` (real mpv, fault
matrix), `test_engine_bridge` (P1+feeder e2e), `test_gui_smoke`, `test_schedule`,
`test_spots`, `test_playlists`, `test_queue_store`, `test_indexer`.
