# Changelog

All notable changes to StudioFire are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.
This file drives the GitHub-release update prompt shown to operators — write entries in
plain English a non-technical operator can understand.

## [Unreleased]

### Added
- Audio Engine (P1) complete first cut: plays music continuously and recovers by itself.
  - Never-silent failover: if the next song can't play, the engine instantly falls back to
    the emergency folder, and if that fails too, to a built-in backup sound.
  - Watches itself every second and auto-restarts the audio player if it hangs or crashes.
  - Remembers exactly where it was across restarts (including emergency mode).
  - Keeps a tamper-proof log of everything that aired, even if the rest of the system is down.
  - Local control connection for the upcoming web interface (play queue, skip, pause/resume).
- Torture-test harness: deliberately abuses the engine (floods of bad commands,
  files corrupted or deleted mid-song, the audio player killed five times in a row)
  and verifies the air never goes quiet for more than 2 seconds. Includes a long-run
  "soak" mode for the 72-hour burn-in before go-live.
- Project scaffold: four-service layout (engine / core / worker / poller), config schema,
  logging locations, and planning docs (PLAN.md v0.3).
