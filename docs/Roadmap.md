---
tags: [reference, studiofire]
status: active
created: 2026-07-15
up: "[[PROJECTS-INDEX]]"
---


[[01-Active-Revenue]]

# Roadmap

Current line is **v1.5.0** (shipped 2026-10-03). Full detail in [[StudioFire/PLAN|PLAN]];
shipped work release-by-release in [[StudioFire/CHANGELOG|CHANGELOG]]. The 2.0 program is in
[[StudioFire/docs/2.0-Design-Decisions|2.0 Design Decisions]].

## Phases
- **Phase 0 — bulletproof engine core** ✅ — mpv supervisor, persisted queue,
  four-source failover, 1s watchdog, torture matrix. No GUI.
- **Phase 1 — web GUI + library** ✅ — FastAPI GUI, playlists, [[StudioFire/docs/Architecture|P3]]
  indexer, library search, on-air cockpit.
- **Phase 2 — studio monitor + IDs** — the [[#Station equipment]] ICMP monitor
  is in; top-of-hour legal IDs ship as a clock-triggered spot rule today. The
  full P4 poller (TX / Barix / UniFi / WireGuard) is still stubbed
  (`services/poller/main.py`).
- **Phase 3 — polish** ✅ (mostly) — **two-deck true crossfade** shipped
  (`engine.crossfade_sec`); song fades; playlist **Shuffle**; **12/24-hour
  clock**; **library duplicate cleanup**. Cart wall and syndication fetch remain.
- **Phase 4 — updates** ✅ — **GitHub-driven self-update** shipped
  (`services/updater.py`, Settings → Software updates), with online DB backup and
  auto-rollback.
- **Phase 5 — StudioFire 2.0** (next, on branch `v2-dev`) — reports (incl. a
  local music air-log export), a thin music-identity layer, duration fixes, and a
  human-reviewed playlist generator. See
  [[StudioFire/docs/2.0-Design-Decisions|2.0 Design Decisions]] and
  [[StudioFire/docs/Release-and-Branches|Release & Branches]].

## Built and working now
- **On-air cockpit** — now playing (real artist/album/title from tags), GO/STOP
  on air, skip, stop-after-song, **Shuffle**, history/log, reports, global
  library search, studio-health pills. **12- or 24-hour clock** (Settings).
- **Editable rotation & shows** — drag-reorder / remove live; base rotation
  edits persist, **show edits apply to that airing only**. Add via Insert Next.
- **Crossfade & fades** — two-deck true crossfade between songs
  (`engine.crossfade_sec`); spots and filler play at full volume, never faded.
- **Playlists** — build/import `.lst`, Shuffle, relink stale paths, mirror
  playlists back out as ZaraRadio `.lst` to a folder; per-song lengths + a file
  browser in the editor.
- **Scheduling** — shows (playlist / single file / folder / `.lst`), once /
  daily / weekly with a run window (stop date). A month **calendar** tab.
- **Spots** — whole folder (rotate) / random-from-folder / single file; every-N,
  clock, schedule, or manual; run windows.
- **Library** — background NAS indexer; **duplicate cleanup** (find copies, move
  the extras to `_Duplicates` with a manifest — never deletes, protects in-use
  files).
- **Updates** — GitHub self-update from Settings → Software updates: verify →
  back up code **and DB** → swap → restart only what changed → auto-rollback.
- **Help** — in-app **Operator's Manual** (top-bar Help link), works offline.
- **Settings** — station folders, playlist `.lst` backup, clock format, What's
  new / change log, **Users** (Admin / Basic; Basic does everything except manage
  users), **Station equipment** (ICMP ping).
- **Ops** — GUI restart, detached launch, auto-start (autologon + logon task),
  [[StudioFire/docs/Operations|soak monitor]].

## Station equipment
Add gear by name + IP; a background pinger shows green/red + latency. First
slice of the [[StudioFire/docs/Architecture|P4]] monitor.

## Known follow-ups
- **Missing track durations** — widespread across the library, not just a VPN
  tag-read artifact. Now owned as a 2.0 workstream (audit first, then a duration
  pipeline with provenance). See
  [[StudioFire/docs/2.0-Design-Decisions|2.0 Design Decisions]] §durations.
- **UNC path portability** — the resolver re-points a stored `Z:\...` at the
  configured NAS root when the drive is absent (v1.2.0+); storing UNC up front is
  still preferred (see [[StudioFire/docs/Gotchas|Gotchas]]).
- **Full P4 monitor** — deeper TX / Barix / UniFi / WireGuard polling beyond the
  ICMP equipment pinger.

## Related
- [[PROJECTS-INDEX]]
