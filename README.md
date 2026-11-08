# StudioFire

**Radio automation that never goes silent.**

StudioFire is self-hosted radio automation for Windows — a modern ZaraRadio
replacement built for small FM, LPFM, and community stations. One installer,
your hardware, your music library. No cloud, no subscription, no internet
required to stay on air.

It has been running 24/7 on air at the reference station since July 2026.

## Why StudioFire

**Zero dead air, by architecture.** The audio engine is a tiny standalone
service with a four-tier failover chain: next cached track → your emergency
folder → cached rotation music → a baked-in fallback asset. Silence requires
four separate failures. Every other part of the system — web GUI, scheduler,
library indexer — can crash, hang, or be updated and the music keeps playing.
The engine watches itself every second and auto-restarts the player if it
hangs; the whole stack was torture-tested (NAS yanked mid-track, player killed
repeatedly, corrupt files queued, power cycles) with a pass bar of *zero
silence longer than 2 seconds over 72 hours*.

**Coming from ZaraRadio? Your playlists just work.** Every StudioFire playlist
IS a `.lst` file on disk — open your existing Zara playlists directly, and
every edit saves straight back to the same file. Paths written on another
machine are translated automatically. Zara and StudioFire can share the very
same playlist files during your transition.

**Built for non-technical operators.** Big buttons, an ON AIR light, plain
English. DJs use a web GUI from any device on the studio LAN; the on-air PC
just runs the services.

## Features

- **On-air cockpit** — now playing (real artist/album/title from tags), GO /
  STOP master switch, skip, stop-after-song, live as-aired history log,
  studio-health badge.
- **Playlists** — Zara-style `.lst` files; build, edit, drag-reorder; global
  library search with "Insert Next" one-off cues; stale-path repair tool.
- **Scheduling** — shows (playlist / single file / folder / `.lst`) once,
  daily, or weekly with run windows; a month calendar view; shows take over at
  their scheduled time at a song boundary and hand back to your rotation.
- **Spots** — station IDs, ads, jingles, PSAs from rotating folders: every N
  minutes, at set minutes past the hour, one-off, or manual. Always inserted at
  song boundaries — music is never cut off.
- **Reports** — as-aired log by date range (all / music / spots) with CSV
  export for sponsor affidavits and regulatory records, built from a
  tamper-resistant play journal the engine writes directly.
- **Library** — background indexer scans your NAS share (MP3 + M4A/AAC) into a
  local search index without hammering the network; songs are pre-cached to
  local disk before airtime so a network hiccup can't interrupt playback.
- **Equipment monitor** — add studio gear by name + IP and get green/red
  status with latency at a glance.
- **Users** — admin and basic roles; LAN-only with session logins.

## Install

**1.** [Download `StudioFire-Setup-<version>.exe`](https://studiofire.io/dl?src=github) — that link always resolves to the newest release and hands the file over from GitHub

**2.** Run it and accept the UAC prompt — that is the only one. The installer
bundles everything (embedded Python, mpv, NSSM); there is nothing else to
install and nothing to configure by hand.

**3.** Follow the setup screens in your browser: station name → admin account →
music folder (a local folder or a share like `\\NAS\Music`) → playlists → the
audio device, with a test tone → done.

That is the whole install. No terminal, no Python, no JSON, no mapped drive.
A full walkthrough of the nine screens, and what each one is really asking, is in
[the setup guide](docs/INSTALL-ACCEPTANCE.md).

Requirements: a Windows 10/11 PC wired to your audio chain, and your music on
a local disk or NAS share.

> **A note on the Windows SmartScreen warning:** the installer is not yet
> code-signed, so Windows will show "Windows protected your PC" on first run.
> Click **More info → Run anyway**. You can verify what you're running — the
> installer is built from this repository (`installer/`), and checksums are
> published with each release.

Installing from source, running the tests, or building a release yourself:
see [docs/DEVELOPING.md](docs/DEVELOPING.md).

## Updates

StudioFire checks for a newer release every few hours and offers it in
**Settings → About & updates**. It never installs anything by itself: an admin
presses Install, the app backs itself up first, and if the new version does not
start cleanly it puts the old one back automatically. Running a newer installer
over an existing install also upgrades in place and keeps your config, playlists,
users and history.

## Telemetry & privacy

Telemetry is **off unless you turn it on**. Nothing leaves the box while it is
off — the tracking tag is not even rendered into the page. What this means in
practice:

- It runs **only in the browser GUI** — the audio engine and playout path
  never send anything, and playback is never affected.
- If you turn it on (the setup wizard asks once, and
  **Settings → About & updates** changes it later), the page reports **which
  features are used**, tagged with a **random install ID** generated once and
  stored in `config\config.json`. Your station name is never sent — nor a file
  path, an IP address, a user name or any song data.
- With no internet connection, the tag simply never loads and the GUI works
  identically.
- An existing config with no `telemetry_enabled` key counts as **off**, so
  upgrading never opts a station in.
- To send the counts to your own GA4 property instead, set
  `"core": { "telemetry_enabled": true, "ga_measurement_id": "<your id>" }`.

## Architecture (the short version)

Four isolated Windows services. The **only** process in the audio path is the
engine.

- **P1 — audio engine**: mpv via JSON IPC, persisted queue, four-tier
  failover, 1-second watchdog, append-only play journal. Never touches the
  database or the network.
- **P2 — core/web**: FastAPI GUI, scheduler, spots, and the feeder that
  pre-caches upcoming tracks from the NAS to local disk.
- **P3 — indexer**: walks the music share into SQLite in the background.
- **P4 — monitor**: equipment status (ICMP today; deeper gear polling on the
  roadmap).

Crash matrix: P2/P3/P4 die → audio unaffected. P1 dies → the service manager
restarts it in seconds and it resumes its persisted queue, mid-show.

## Support

StudioFire is developed by [VeteranOp, LLC](https://veteranop.com), a
veteran-owned IT and managed-services company.

- **Your own station's support contact** — set it in **Settings → Station →
  Support contact**; it appears on the Help page and in error banners, so
  volunteers know who to call without hunting.
- **Community** — [GitHub Issues](https://github.com/veteranop/StudioFire/issues)
  for bugs, [Discussions](https://github.com/veteranop/StudioFire/discussions)
  for questions and ideas.
- **Paid support** — installation, configuration and managed operation for your
  station: [contact VeteranOp](https://veteranop.com).

The full support policy is in [SUPPORT.md](SUPPORT.md). StudioFire is provided
as-is under the licence below — there is no warranty and no guaranteed response
time unless you have a support agreement.

## License

[MIT](LICENSE) © 2026 VeteranOp, LLC
