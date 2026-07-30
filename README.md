# StudioFire

**Radio automation that never goes silent.**

StudioFire is self-hosted radio automation for Windows — a modern ZaraRadio
replacement built for small FM, LPFM, and community stations. One installer,
your hardware, your music library. No cloud, no subscription, no internet
required to stay on air.

It has been running 24/7 on air at KDPI since July 2026.

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

**[Download the latest `StudioFire-Setup-<version>.exe` from Releases](https://github.com/veteranop/StudioFire/releases/latest).**

The installer bundles everything (embedded Python, mpv, NSSM) — nothing to
pre-install. The wizard asks for your station name and music folder, can
register the auto-restarting Windows services, and opens the firewall for the
web GUI. Upgrades keep your config, playlists, and data.

Requirements: a Windows 10/11 PC wired to your audio chain, and your music on
a local disk or NAS share. If the music lives on a NAS and you run StudioFire
as Windows services, see [DEPLOY.md](DEPLOY.md) for the service Log On +
drive-mapping step.

> **A note on the Windows SmartScreen warning:** the installer is not yet
> code-signed, so Windows will show "Windows protected your PC" on first run.
> Click **More info → Run anyway**. You can verify what you're running — the
> installer is built from this repository (`installer/`), and checksums are
> published with each release.

### Running from source (dev)

```
pip install -r requirements.txt   # Python 3.12
start-all.bat                     # engine + core/web + indexer
healthcheck.bat                   # is it on air?
```

Web GUI: `http://<machine>:8080` — first run creates the admin account.

## Updates

There is no auto-updater yet. Updates ship as new installers on the
[Releases page](https://github.com/veteranop/StudioFire/releases) — watch the
repo to get notified. Running a newer installer over an existing install
upgrades in place and keeps your config and data. A built-in update check is
on the roadmap.

## Telemetry & privacy

The web GUI includes Google Analytics (GA4) usage telemetry so we can see
which features stations actually use. What this means in practice:

- It runs **only in the browser GUI** — the audio engine and playout path
  never send anything, and playback is never affected.
- The data is feature usage (pages and actions in the GUI), with your
  **station name** attached so we can tell deployments apart. No listener
  data, no audio, no library contents.
- With no internet connection, the tag simply never loads and the GUI works
  identically.
- **Opting out** is one line in `config\config.json`:
  `"core": { "ga_measurement_id": "" }` — or set your own GA4 measurement ID
  there to send usage data to your own property instead.

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
veteran-owned IT and managed-services company. Open an
[issue](https://github.com/veteranop/StudioFire/issues) for bugs and feature
requests — or contact us if you'd like StudioFire installed, configured, and
managed for your station.

## License

[MIT](LICENSE) © 2026 VeteranOp, LLC
