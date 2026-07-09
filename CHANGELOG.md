[[01-Active-Revenue]]

# Changelog

All notable changes to StudioFire are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.
This file drives the GitHub-release update prompt shown to operators — write entries in
plain English a non-technical operator can understand.

## [Unreleased]

### Added
- Google Analytics (GA4) usage telemetry in the web GUI, so we can see which
  features stations actually use. The station name is attached to the data.
  It never affects playback and the GUI works identically with no internet.
  A station can opt out (or use its own GA property) with
  `"core": {"ga_measurement_id": ""}` in config.json.
- A Windows installer (installer\StudioFire.iss + build_payload.py): one
  setup.exe that bundles Python, mpv, and NSSM — nothing to pre-install on a
  customer PC. The wizard asks for the station name and music folder, can
  register the auto-restarting Windows services, and opens the firewall for
  the web GUI. Upgrades keep the existing config and data.
- Emergency audio no longer needs hand-picked filler files. If the emergency
  folder is empty, the engine plays real music from its local song cache
  instead — listeners hear normal songs, not a repeating clip. (Adding files
  to `assets\emergency\` still works and takes priority — useful for station
  IDs or "technical difficulties" messages.)
- A ⟳ "restart everything" button next to the ON AIR light (and restart-all.bat)
  to manually cycle all services during testing. Guarded by a config flag
  (allow_gui_restart) so it can be turned off on the on-air PC.
- Now Playing and the History / Log now show the real Artist / Album / Song,
  read straight from each song's tags (via its cached copy) — no more cache-hash
  gibberish in the log, and track times are accurate too.
- "Fix broken file paths" on the Playlists page: many playlists (imported before
  the NAS move) point at old locations — this repoints each stale track to the
  real file in your library, matched by name. No re-import needed.

### Fixed
- Intermittent errors on every page under load — the database connection wasn't
  safe to hand between the web server's worker threads. Fixed.
- A rare file-lock error while caching songs ahead (which also quietly skipped a
  feed cycle now and then) is gone.

### Changed
- The Playlists page is now just three simple actions: start a new playlist,
  open one you have, or import a ZaraRadio .lst — no more busy table of
  buttons. Duplicate, Delete, and PUT ON AIR live inside the playlist editor,
  where you can see exactly what you're acting on. The full playlist
  export/restore moved to Settings (admins only).
- On Air controls are simpler and clearer. The 🚨 EMERGENCY button is gone;
  the three buttons are now GO / STOP — On Air (one master switch: STOP takes
  you off air immediately, GO puts you back), Skip Song, and Stop after
  current Song (finishes the song that's playing, then goes off air with the
  next song cued and ready — press GO to continue). Automatic emergency filler
  still kicks in on its own if the playlist ever can't continue.
- The middle On Air list now follows whatever is actually on air: when a show is
  playing it shows the SHOW's playlist (with the on-air song marked), and it goes
  back to your rotation when the show ends — so the list always matches Now
  Playing. It's read-only while a show is on air (you edit your rotation, not a
  one-time show), with a "SHOW ON AIR" badge.
- Scheduled shows now always take over at their time (a running show ends early),
  so a long show can't block later scheduled programming; a "Stop show" button
  ends a show and returns to the rotation. Start now (cut immediately) vs Cue
  next (after the current song) are separate per-entry actions.
- The rotation list now pins the on-air song to the top (it stays stuck there as
  you scroll) and hides the songs already played this pass, so you can always see
  where you are and what's coming. Reordering applies to the upcoming songs.
- Library search now finds your whole indexed library, including deeply nested
  Artist/Album/Song folders, and still lists tracks a scan flagged as unreachable
  (marked "offline?") so a flaky NAS can't hide them from search.
- Indexer no longer flags tracks missing when their folder simply couldn't be
  read this pass (slow/hidden NAS subfolder) — only when the folder was readable
  and the file was genuinely gone. Prevents a bad scan from wiping the library.
- The On Air cockpit now uses the full width of a wide monitor instead of a
  cramped 1600px centre column: the rotation and History panels get real room,
  the spot rows stop wrapping, and song titles fit on one line. (Other pages
  keep their comfortable reading width.)
- The middle On Air column now shows the WHOLE rotation playlist (not just the
  pre-cached next ~10), with the song that's on air marked and auto-scrolled
  into view, and a search box to jump around a long list. Drag to reorder or
  remove a song and it's saved to the playlist for good — and the change takes
  effect on air immediately (the current song keeps playing; everything after
  it re-syncs to your edit). No more editing a throwaway buffer.
- The On Air page got a professional facelift: the StudioFire logo now sits in
  the top-left, the three columns (Upcoming spots, Coming up, Playlists on air)
  have room to breathe instead of feeling cramped, section headers are cleaner,
  and the long "Coming up" list scrolls within its panel so the page stays tidy.

### Added
- Reports page: pick a date range and see exactly what aired — everything, music
  only, or spots only (Station IDs / ads / PSAs, i.e. proof of performance) — with
  a one-click CSV export for affidavits/logs. Built from the as-aired play journal.
- Global library search on the On Air page: search your whole music library and
  drop any song in live with "Insert Next" — a one-off cue that plays right
  after the current song, without touching the saved rotation playlist.
- A big live clock in the top bar (every page) — radio runs on the wall clock.
- History / Log panel on the On Air page: a live as-aired record of what
  actually went out (song started/ended, spot played, filler), colour-coded
  and timestamped, straight from the play journal. The On Air screen is now a
  two-zone cockpit — work area on the left, the log rail down the right.
- Spots — Station IDs, ads, jingles, PSAs — now schedule themselves between
  songs. A new "Upcoming spots" column on the left of the On Air page shows
  what's coming with a live countdown. Add a rule pointing at one of your
  Settings folders and choose when it fires: every N minutes, at set minutes
  past the hour (e.g. a legal Station ID at :00), a one-off date/time, or a
  manual "Play now" button. Files rotate evenly through the folder, and every
  spot slots in at the end of the current song so music is never cut off.
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
