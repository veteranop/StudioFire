---
tags: [moc, studiofire]
status: active
created: 2026-07-15
up: "[[PROJECTS-INDEX]]"
---


[[01-Active-Revenue]]

# StudioFire — Home

Radio-automation platform (a ZaraRadio replacement) for a small FM station.
Python (Anaconda), local-first, "audio is everything." This note is the map of
the project. The Obsidian vault is your **Desktop**; this project lives under
`StudioFire/`, so links here are written as `StudioFire/…` and resolve no matter
what other projects are in the vault.

## Start here
- [[StudioFire/docs/Architecture|Architecture]] — the four isolated services and why
- [[StudioFire/docs/Roadmap|Roadmap]] — phases and what's built so far
- [[StudioFire/docs/Operations|Operations]] — running, restarting, deploying, the soak test
- [[StudioFire/docs/Gotchas|Gotchas]] — hard-won lessons (read before touching the audio path)
- [[StudioFire/docs/Release-and-Branches|Release & Branches]] — branch model, release process, the major-version gate
- [[StudioFire/docs/2.0-Design-Decisions|2.0 Design Decisions]] — the 2.0 program as decided (MVP spine, local-first logging, release safety)
- [[StudioFire/docs/Training-Guide-Best-Practices|Training Guide — Best Practices]] — how to build the operator training PDF + LMS modules

## Reference docs (repo root)
- [[StudioFire/PLAN|PLAN]] — the full binding spec (§10 is the engine contract)
- [[StudioFire/CHANGELOG|CHANGELOG]] — what changed, release by release
- [[StudioFire/DEPLOY|DEPLOY]] — deploying to the on-air / production box
- [[StudioFire/REVIEW|REVIEW]] — the doc handed to Grok for second opinions
- [[StudioFire/README|README]]

## The one-paragraph version
Four Windows services, isolated so a failure in one can't take air off:
**P1 audio engine** (the only thing in the audio path — two mpv decks via named
pipes, persisted queue, four-source failover, true crossfade, never touches the
DB), **P2 core/GUI** (FastAPI + the feeder that pre-caches NAS files locally;
playlists, scheduling, spots, reports, library search and the Help manual live
here), **P3 indexer** (walks the NAS into SQLite), **P4 monitor** (not built
yet — the equipment ICMP pinger is the shipped early slice). DJs just browse to
P2's web GUI. See [[StudioFire/docs/Architecture|Architecture]].

## Related
- [[PROJECTS-INDEX]]
