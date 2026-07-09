[[01-Active-Revenue]]

# Architecture

Mandate: **segmented — "audio on is everything."** Four isolated Windows
services so nothing outside the audio path can ever cause dead air. Full spec in
[[StudioFire/PLAN|PLAN]] §5–§10.

## The four services
- **P1 — audio engine** (`services/engine/`) — the ONLY process in the audio
  path. Thin supervisor + `mpv` over a Windows named pipe (JSON IPC). Control
  HTTP on `127.0.0.1:7701`. Persisted queue, **local files only**, zero deps on
  the DB / NAS / network. Owns the play journal. Stdlib only.
- **P2 — core / GUI** (`services/core/`) — FastAPI web app on `:8080` + the
  **feeder** that resolves playlists/shows/spots and pre-caches NAS files to a
  local cache (~45 min buffer), then feeds P1 via the queue protocol. SQLite
  (WAL). DJs browse here.
- **P3 — indexer** (`services/worker/`) — throttled background NAS walk into
  SQLite (mutagen tags). Two-phase: fast path walk, then tag backfill. See
  [[StudioFire/docs/Gotchas|Gotchas]].
- **P4 — monitor** — separate poller for TX / Barix / UniFi. Not built yet;
  the [[StudioFire/docs/Operations|Station equipment]] ICMP monitor is an early piece.

## Key contracts
- **queue_version protocol** — monotonic; P1 rejects a stale mutation with 409,
  P2 re-syncs. P1 is the single writer of its queue state.
- **3-tier failover** — precache → emergency folder → baked-in tone. A 1s
  watchdog checks the play-head is advancing.
- **Show overlay** — a scheduled show interrupts the base rotation, plays once
  through, then hands back (`active_playlist_id`). Items are snapshot into the
  feeder overlay so they're [[StudioFire/docs/Roadmap|editable live]].
- **Path aliases** — playlists store `\\KDPI-Media\music\…`; each box aliases it
  to its local mount (`Z:` at home). See [[StudioFire/docs/Gotchas|Gotchas]].

## Data
SQLite schema is versioned ([[StudioFire/docs/Operations|migrations]] run at startup). P1 never
touches it. The library index (`tracks`), playlists, schedule, spots, devices,
and settings all live here.
