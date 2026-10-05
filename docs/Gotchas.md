---
tags: [reference, studiofire]
status: active
created: 2026-07-15
up: "[[PROJECTS-INDEX]]"
---


[[01-Active-Revenue]]

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
- **Store portable paths.** Prefer a UNC root like `//KDPI-Media/music/G` in
  `paths.nas_music_root`; the on-air PC should not depend on a home-only `Z:`.
  Don't rewrite playlist paths to `Z:` (relink would).
- **Bash heredocs mangle backslashes** — write Python to a file, never inline a
  heredoc with Windows paths.

## Restart / deploy
- **GUI restart button** uses `scripts/restart_all.py` (detached Python), NOT
  the `.bat` — `start cmd /k` can't spawn windows from P2's no-console process.
  See [[StudioFire/docs/Operations|Operations]].

## Deploy / audio session (the biggest one)
- **A Windows service runs in session 0, which has NO audio endpoint.** Run the
  engine as a service and mpv launches, `GET /health` returns `ok`,
  `services.msc` shows Running — and **every track fails** with
  `audio output initialization failed`. Proven live on `.200` (2026-09-29): 3,529
  silent "plays" with green health. There is no supported way to give a WASAPI
  app an endpoint in session 0. **Never run an audio host as a service** — use
  autologon + an at-logon task (`scripts\install-autostart.bat`). HTTP 200 is not
  proof of on-air; listen to the stream. Full evidence:
  `docs/AUTOSTART-CONSENSUS.md`; runbook: `docs/ON-AIR-SETUP.md`.

## Feeder concurrency (P2)
- **One writer of feeder state.** `Feeder.tick()` and every insert/edit path
  share `self._lock` (RLock); a read-modify-write of `feeder_state` without it
  was a real lost-update that silently dropped cued spots/tracks. Slow NAS
  copies happen *before* taking the lock, never inside it.
- **Pin invariant:** an item the engine has accepted (202) must not be removed
  from the feeder model before it is observed as started or ended — otherwise the
  same-tick eviction pass deletes its precached file and P1 skips it at prefetch
  ("unplayable at prefetch"). This is how hourly IDs "fired but never aired."

## Related
- [[StudioFire/docs/Operations|Operations]]
- [[PROJECTS-INDEX]]
