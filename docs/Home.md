# StudioFire — Home

Radio-automation platform (a ZaraRadio replacement) for a small FM station.
Python (Anaconda), local-first, "audio is everything." This note is the map of
the project — open the **project root** as an Obsidian vault and start here.

## Start here
- [[Architecture]] — the four isolated services and why
- [[Roadmap]] — phases and what's built so far
- [[Operations]] — running, restarting, deploying, the soak test
- [[Gotchas]] — hard-won lessons (read before touching the audio path)

## Reference docs (repo root)
- [[PLAN]] — the full binding spec (§10 is the engine contract)
- [[CHANGELOG]] — what changed, release by release
- [[DEPLOY]] — deploying to the on-air / production box
- [[REVIEW]] — the doc handed to Grok for second opinions
- [[README]]

## The one-paragraph version
Four Windows services, isolated so a failure in one can't take air off:
**P1 audio engine** (the only thing in the audio path — mpv via a named pipe,
persisted queue, 3-tier failover, never touches the DB), **P2 core/GUI**
(FastAPI + the feeder that pre-caches NAS files locally), **P3 indexer** (walks
the NAS into SQLite), **P4 monitor** (not built yet). DJs just browse to P2's
web GUI. See [[Architecture]].
