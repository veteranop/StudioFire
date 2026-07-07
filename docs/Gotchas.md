# Gotchas

Hard-won lessons. Read before touching the audio path or the indexer.

## Audio path
- **Every-other-track skip (fixed 2026-07-05).** The mpv playlist-trim removed
  the CURRENT entry after an eof-advance, silently skipping ~half of queued
  tracks/spots. Never caused silence, so the soak gate missed it — add
  **content-skip** checks to soak tests, not just silence. Fixed in
  `supervisor._ensure_next_appended` (keep current, trim the rest).
- **Windows named-pipe deadlock.** A blocking `readline()` in one thread
  deadlocks `write()` from another on the same handle. `mpv_ipc.py` uses a
  single I/O thread + `PeekNamedPipe` polling + an outgoing write queue. Do not
  regress to a reader-thread + writer pattern.
- **mpv `time-pos` is "unavailable" while a file loads** — poll for it, don't
  treat it as dead.
- **Queue history is trimmed to the last 20** played entries at runtime; the
  play journal is the permanent record.

## Indexer
- **Alphabetical scan must finish.** Missing T–Z artists = a scan that never
  completed a full A→Z walk (killed at session/VPN boundaries). Don't kill P3
  mid-scan to "fix" missing artists — that resets it to A.
- **Two-phase + scandir.** Phase 1 records paths (searchable fast), phase 2
  backfills tags. `os.scandir` gives free `stat()` on Windows (no per-file SMB
  round-trip). See [[StudioFire/docs/Architecture|P3]].
- **`._` AppleDouble junk** carries audio extensions but no audio — filter it.

## Network / paths
- **VPN latency, not a slow NAS.** ~30 ms/op over OpenVPN made a folder listing
  take ~176 s; on-site LAN it's instant. Don't chase SMB tuning for it.
- **Store portable paths.** `Z:` is home-only; the on-air PC uses
  `\\KDPI-Media\music\…`. Don't rewrite playlist paths to `Z:` (relink would).
- **Bash heredocs mangle backslashes** — write Python to a file, never inline a
  heredoc with Windows paths.

## Restart / deploy
- **GUI restart button** uses `scripts/restart_all.py` (detached Python), NOT
  the `.bat` — `start cmd /k` can't spawn windows from P2's no-console process.
  See [[StudioFire/docs/Operations|Operations]].
