[[01-Active-Revenue]]

# Roadmap

Phases (v0.2, post-review). Full detail in [[StudioFire/PLAN|PLAN]]; shipped work in [[StudioFire/CHANGELOG|CHANGELOG]].

## Phases
- **Phase 0 — bulletproof engine core** ✅ — mpv supervisor, persisted queue,
  3-tier failover, 1s watchdog, torture matrix. No GUI.
- **Phase 1 — web GUI + library** ✅ — FastAPI GUI, playlists, [[StudioFire/docs/Architecture|P3]]
  indexer, library search, on-air cockpit.
- **Phase 2 — studio monitor + IDs** — the [[#Station equipment]] ICMP monitor
  is in; top-of-hour IDs / full P4 poller still to come.
- **Phase 3 — polish** — cart wall, crossfade, richer scheduling, syndication
  fetch.
- **Phase 4 — updates** — GitHub-driven self-update.

## Built and working now
- **On-air cockpit** — now playing, GO/STOP on air, skip, stop-after-song,
  history/log, reports, global library search, studio-health pills.
- **Editable rotation & shows** — drag-reorder / remove live; base rotation
  edits persist, **show edits apply to that airing only**. Add via Insert Next.
- **Playlists** — build/import `.lst`, relink stale paths, mirror playlists back
  out as ZaraRadio `.lst` to a folder.
- **Scheduling** — shows (playlist / single file / folder / `.lst`), once /
  daily / weekly with a run window (stop date). A month **calendar** tab.
- **Spots** — whole folder (rotate) / random-from-folder / single file; every-N,
  clock, schedule, or manual; run windows.
- **Settings** — station folders, playlist `.lst` backup, **Users** (Admin /
  Basic; Basic does everything except manage users), **Station equipment**
  (ICMP ping).
- **Ops** — GUI restart, detached launch, [[StudioFire/docs/Operations|soak monitor]].

## Station equipment
Add gear by name + IP; a background pinger shows green/red + latency. First
slice of the [[StudioFire/docs/Architecture|P4]] monitor.

## Known follow-ups
- Missing durations for many tracks (tag reads flaky over VPN — clean on-site
  re-scan fills them in).
- Canonicalize playlist paths to UNC for on-air portability (deliberate,
  backed-up pass — see [[StudioFire/docs/Gotchas|Gotchas]]).
