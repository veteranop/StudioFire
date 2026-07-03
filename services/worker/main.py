"""StudioFire P3 — Worker Service.

Throttled, low-OS-priority background jobs, isolated so they can never touch
playback or the GUI:
- Library indexer: incremental NAS scan (mutagen tags -> SQLite), file watcher.
- Syndication fetcher (Phase 3): SFTP/FTP show downloads, temp -> verify ->
  atomic rename, retention pruning.

Run: python -m services.worker.main  (Windows service via NSSM). Phase 1/3.
"""

raise SystemExit("P3 worker: not implemented yet (Phase 1)")
