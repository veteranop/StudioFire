# Changelog

All notable changes to StudioFire are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.
This file drives the GitHub-release update prompt shown to operators — write entries in
plain English a non-technical operator can understand.

## [Unreleased]

### Added
- Schedule and cue whole playlists from the On Air page. The right-hand
  "Playlists on air" panel shows what rotation is on now and an "Up next"
  list of shows coming up. Add a playlist with a start time and it takes over
  automatically at that time (at the next song boundary); leave the time blank
  and press "Start now" when you want it. A show plays once through, then hands
  back to your regular rotation — with a red "SHOW ON AIR" banner while it runs.
- The "Coming up" list on the On Air page is now hands-on: click any song for
  Play now / Cue next / Remove, or drag songs up and down to reorder the queue
  on the fly. The song playing right now is never disturbed by these edits.
- Big blinking ON AIR light at the top of the screen: glows red while audio is
  actually going out, goes dim to OFF AIR when paused or nothing is playing.
- Studio health moved to a small colored badge at the top (next to Sign out).
  It's green when all is well, turns yellow or red if anything needs attention;
  click it to drop down the details (music library, disk space, index).
- Now Playing shows the real song name (not a cryptic cache filename) and the
  time: how far in, how long the song is, and how much is left.
- Settings page (admin only): point StudioFire at your station folders —
  Shows, Advertisements, Station IDs, Jingles, PSAs — with a built-in folder
  browser, no typing paths. These will drive automatic scheduling next.
- Import your old ZaraRadio playlists: upload a .lst file on the Playlists
  page and it becomes a normal StudioFire playlist. Paths written on another
  computer (like \\KDPI-Media\music) are automatically translated to where
  the music lives on this machine.
- Small fixes from first hands-on use: pressing Enter now creates the
  playlist, and empty inputs tell you what to do instead of doing nothing.
- EMERGENCY button on the On Air page: one press puts the emergency filler on
  air immediately and keeps it there — the automation will NOT sneak back in —
  until you press RESUME NORMAL. Survives restarts of the audio engine.
- Backup & restore on the Playlists page (admin only): download one file with
  every playlist in it; restore it later on this or another machine. Restoring
  never overwrites — same-named playlists come back as "(restored)" copies.
- Web control room (P2) first cut: sign in from any device on the studio network.
  - On Air page: what's playing now, what's coming up, big PAUSE AUTOMATION /
    RESUME and Skip buttons, and studio health tiles (music library reachable,
    disk space, library index).
  - Playlists: create, edit, reorder, duplicate, and "PUT ON AIR" with one click.
    Playlists can include smart items: "newest file from a folder" (syndicated
    shows) and "rotate through a folder" (ad spots).
  - "Play Next": cue any song to play right after the current one.
  - First-run setup page creates the admin account; operators get their own logins.
- Behind the scenes: songs are copied from the NAS to a local cache before they
  air, so a network hiccup can never interrupt a song mid-play. Everything that
  airs is recorded permanently for sponsor/as-aired records.
- Library indexer (P3): scans the NAS music share in the background and keeps
  the search index fresh without hammering the network.
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
