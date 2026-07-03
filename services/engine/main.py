"""StudioFire P1 — Audio Engine Service.

THE ONLY PROCESS THAT MUST STAY ALIVE. Binding spec: PLAN.md §10.

Design laws (do not violate):
- STDLIB ONLY. No fastapi, no sqlite3 usage, no third-party imports.
  The only external dependency is mpv.exe, controlled via JSON IPC.
- Plays ONLY local files: precache dir -> emergency folder -> baked-in asset.
- Queue persisted atomically (temp + os.replace) on every mutation and advance:
  {queue_version, entries, current_index, emergency_mode, timestamp}
- Never blocks on IPC with P2. If P2 vanishes, play out cache, then emergency loop.
- Appends every play event to a local JSONL journal the instant it happens.

Modules:
    supervisor.py  - main loop, 1s watchdog (IPC responsive + position advancing),
                     mpv restart, failover chain, heartbeat file
    mpv_ipc.py     - mpv process launch + JSON IPC client (named pipe on Windows)
    queue_store.py - atomic queue persistence, queue_version protocol
    journal.py     - append-only JSONL play journal
    control.py     - localhost-only control endpoint for P2 (queue ops, status)

Run: python -m services.engine.main  (installed as a Windows service via NSSM)
"""

raise SystemExit("P1 engine: not implemented yet (Phase 0)")
