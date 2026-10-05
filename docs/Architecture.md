---
tags: [reference, studiofire]
status: active
created: 2026-07-15
up: "[[PROJECTS-INDEX]]"
---


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
  **feeder** that resolves playlists/shows/spots, pre-caches NAS files to a
  local disk cache (~45 min, `core.precache_target_minutes`), then feeds P1 via
  the queue protocol. **Feed depth is decoupled from cache depth:** the feeder
  only queues `core.feed_ahead_tracks` (default 3) ahead in P1, while the 45-min
  *disk* cache is the NAS-outage buffer — so if P2 dies, P1 drains fast, enters
  emergency, and tier 3 below plays real rotation music from the still-full
  precache dir. SQLite (WAL). DJs browse here.
- **P3 — indexer** (`services/worker/`) — throttled background NAS walk into
  SQLite (mutagen tags). Two-phase: fast path walk, then tag backfill. See
  [[StudioFire/docs/Gotchas|Gotchas]].
- **P4 — monitor** — separate poller for TX / Barix / UniFi. Not built yet;
  the [[StudioFire/docs/Operations|Station equipment]] ICMP monitor is an early piece.

## Key contracts
- **queue_version protocol** — monotonic; P1 rejects a stale mutation with 409,
  P2 re-syncs. P1 is the single writer of its queue state.
- **Four-source failover** (§10.1) — on any "can't start the next track," the
  engine falls through a fixed chain: **(1)** next pre-cached queue item →
  **(2)** emergency-folder loop → **(3)** cached rotation music in the precache
  dir (scanned fresh each time, since its contents churn) → **(4)** a baked-in
  last-resort source (an `av://lavfi` tone by default, or a configured
  `engine.baked_in_asset`). It exits emergency the instant a playable queue item
  exists again. A 1 s watchdog checks mpv liveness **and** that the play-head is
  advancing (to catch a silent hang where mpv is "alive" but frozen).
- **Two-deck crossfade** — two mpv players alternate; the mixer thread ramps
  volume so songs overlap (`engine.crossfade_sec`, default 4; 0 = back-to-back).
  Spots and emergency/baked-in filler never crossfade.
- **Show overlay** — a scheduled show interrupts the base rotation, plays once
  through, then hands back (`active_playlist_id`). Items are snapshot into the
  feeder overlay so they're [[StudioFire/docs/Roadmap|editable live]].
- **Path aliases** — playlists store `\\KDPI-Media\music\…`; each box should
  prefer a local UNC root such as `//KDPI-Media/music/G` in `paths.nas_music_root`.
  Legacy mapped-drive aliases like `Z:` are only for compatibility. See [[StudioFire/docs/Gotchas|Gotchas]].

## Data
SQLite schema is versioned ([[StudioFire/docs/Operations|migrations]] run at startup). P1 never
touches it. The library index (`tracks`), playlists, schedule, spots, devices,
and settings all live here.

## Related
- [[PROJECTS-INDEX]]
