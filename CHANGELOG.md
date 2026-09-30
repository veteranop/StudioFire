# Changelog

All notable changes to StudioFire are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.
This file drives the GitHub-release update prompt shown to operators — write entries in
plain English a non-technical operator can understand.

## [Unreleased]

### Added
- **Auto-start that keeps the station on air: `scripts\install-autostart.bat`.**
  Run it as Administrator on the box and the station comes back by itself after a
  power bump or a Windows restart, with the sound still working and nobody at the
  keyboard. It turns on Windows autologon for the station account and creates two
  scheduled tasks: one that starts `start-all.bat` shortly after that account logs
  in, and a watchdog that checks every 5 minutes and restarts the stack if it stays
  unhealthy twice in a row. Add `-check` to see exactly what it would do without
  changing anything; `scripts\remove-autostart.bat` undoes it.

### Changed
- **`install-services.bat` now refuses to run on a station.** A Windows service runs
  in session 0, which has no audio device: the engine starts, plays nothing, and the
  old script still reported success while `services.msc` showed the service Running.
  It now stops and points you at `install-autostart.bat` instead. If you genuinely
  want the services on a box that never makes sound, pass `-i-understand-no-audio`.
- `DEPLOY.md` no longer recommends services as the production setup for a station —
  see `docs/AUTOSTART-CONSENSUS.md` for the evidence and the reasoning.

## [1.3.2] - 2026-09-29

### Changed
- **Auto-start is now one .bat, and it doesn't leave the fiddly part to you.**
  `scripts\install-services.bat` (run as Administrator on the on-air PC) registers
  the three services so they start at boot and restart themselves if they crash,
  asks for the Windows account the station should run as, then starts them and
  reports which ones came up. Add `-check` to see exactly what it would do with no
  changes at all. The Log On account matters because a Windows service session has
  no drive letters, no user profile and no NAS credentials — run as LocalSystem and
  the station comes back and finds no music. If Windows refuses the account or the
  password the script says so instead of leaving you with services sitting Paused.
  It also stops any console-mode stack first, so you can't end up with two engines
  on one station.

## [1.3.1] - 2026-09-29

### Fixed
- **Spots (legal IDs, PSAs, ads, jingles) that fired but never actually played.**
  A fired spot was correctly handed to the player, but a housekeeping pass could
  briefly forget about it and then delete its ready-to-play copy from the local
  cache before it reached the air — so the spot was silently skipped and the next
  song/show played instead (seen on the KDPI on-air PC on 2026-09-29: rules fired
  every hour but nothing aired). StudioFire now holds on to a just-queued spot or
  cued track until the player confirms it, so it can't be dropped before it plays.

## [1.3.0] - 2026-09-29

### Added
- **A "What's new / Change log" section in Settings.** It shows the version your
  station is running, what changed in it, and the versions before it (newest
  first, older ones fold open) — so after an update you can see exactly what came
  in without having to ask. Nothing to set up: it reads the change log that
  already ships with StudioFire.

## [1.2.0] - 2026-09-29

### Fixed
- **The station no longer needs a mapped drive (like `Z:`) to find its music,
  playlists, station IDs, PSAs or ads.** A mapped drive only exists while
  someone is logged in, so after a reboot or a Windows update the station could
  come back up without it — and then the file browser showed only `C:\`, the
  station folders in Settings read "does not exist", and scheduled items could
  silently miss their windows (on 2026-09-29 a Windows-update reboot dropped the
  drive on the KDPI on-air PC and the legal-ID, PSA and underwriter rules missed
  every window for the rest of the afternoon). StudioFire now finds those files
  through the music library's own address instead of the drive letter, so
  playlists, shows, IDs and PSAs keep working whether the drive is mapped or not.
- **The file browser can now reach the music library even with no drive
  mapped.** At the top level the picker lists the library itself alongside the
  local drives, so you can always browse to your music and your `.lst`
  playlists. Opening a path that was saved with a drive letter (e.g. `Z:\...`)
  also works when that drive isn't there any more.
- **Restarting or updating re-connects the drive automatically** when the
  station has a `config\drive-map.bat` — every way StudioFire starts or restarts
  now runs it first (previously only the Windows-service path did). If the
  mapping fails, the station still starts; playback is never held up by it.

## [1.1.0] - 2026-09-27

### Added (operator feedback from KDPI, 2026-09-25, TimeTrax #644)
These came from John after the first live show on the new system.
- **Songs crossfade.** In the last 4 seconds of a song, the next song
  starts underneath it and rises while the old one falls, with no gap and no
  dip in the middle (an "equal-power" fade, so the overall loudness stays
  steady all the way through). PSAs, ads and station IDs are never faded. One can start
  over a song's fade-out, but it plays at full volume from start to finish,
  and the song after it starts the moment it ends, at full volume.
  Emergency filler is never faded either. Skip is still an instant cut, and
  "Stop after current song" lets the song finish fully before it stops. The
  crossfade length is set in `config.json` under `engine` → `crossfade_sec`
  (0 turns crossfading off, so items play back to back).
- **Program Output meter (VU) at the top of the On Air page.** Live left and
  right levels of what the station is sending to the sound card, with
  green/amber/red zones, peak-hold marks and a peak readout in dB. If the
  station goes silent while on air for 8 seconds, the meter card turns red
  and says so. The level is measured without changing the audio. If the meter
  can't run, it says "Meter offline" and the music plays on normally.
- **Redesigned Now Playing card:** bigger title, a progress bar, the
  playlist/show line, and the Stop, Skip and Stop-after buttons stacked on the
  right. The card's edge is green while on air and grey when stopped.
- **Search spots by name.** Type in the new search box above Upcoming Spots
  to find a spot by its name, schedule, or file/folder.
- **Date next to the clock** at the top of every page (e.g. "Fri, Sep 25").
- **Now Playing shows the playlist.** The card now shows which playlist or
  show is on air, how many songs are left, how much time is left, and roughly
  when it will end.
- **Song count and run time for playlists.** The playlist page shows how many
  songs it has and how long it plays, and each song shows its length. Songs
  whose length isn't known yet are counted separately; their length fills in
  automatically the first time they're readied for air.
- **File browser on the playlist page.** Open any folder the studio PC can
  see (NAS included) and use **+ Add** to add a song to the playlist or
  **▶ Next** to play it right after the current song. There's also an
  "Add all songs in this folder" button. It reopens the last folder you used.
  Adding songs no longer reloads the page, so you can add several in a row.

- **StudioFire can update itself from GitHub.** Settings → **Software
  updates** shows your version and whether a newer one has been published,
  with its release notes, and a green **⬆ Update** pill appears on the On
  Air page when one is available. An admin presses **Install update**
  (StudioFire never installs anything by itself). It backs up first,
  restarts only what the update changed (most updates don't interrupt the
  music at all), and if the new version doesn't start properly it puts the
  old one back by itself. You can also run **Update StudioFire from GitHub**
  from the Start menu.

### Changed
- **The On-Air schedule's "Up next" list is in true air order.** Each entry
  now starts with when it will actually air next (e.g. "▶ Tomorrow 6:00 AM").
  Before, every one-time show was listed ahead of every repeating one, so a
  daily 6 AM show could appear below something scheduled for next week.
- **Better on smaller monitors.** The On-Air schedule and Upcoming Spots
  lists wrap long names, and their buttons move under the text instead of
  getting cut off. The Now Playing buttons shrink to fit, and the History/Log
  column moves below the main area on screens narrower than 1440 pixels. The
  clock no longer overlaps the menu.
- **Calendar names stay in their box.** Long show names wrap inside the day
  box instead of spilling into the next day or getting cut off.
- After an update, the page picks up the new layout by itself. Before, the
  browser could keep showing the old layout until you did a hard refresh.

### Fixed
- Saving a playlist from StudioFire no longer blanks out the song lengths
  stored in its .lst file.
- A playlist containing a file name with an unusual character (e.g. "√")
  no longer breaks when saved. The character used to be turned into "?", and
  that song would then never play again.
- A malformed length in a .lst file (e.g. "-2") no longer causes that line's
  song to be skipped.
- Playlists made on another computer now play on this one when a path alias
  is set up (`path_aliases` in config.json). Aliases used to apply only while
  importing a playlist, so a rotation that was already saved could fail to
  play and drop the station into emergency filler.
- If the audio engine is stopped abruptly (e.g. Ctrl+C in its window), its
  player no longer keeps running in the background and gets mixed up with the
  next engine.
- Recovery after the audio player crashes is faster: it no longer waits 2
  seconds on a player that's already gone.
- Closed a race in the web control room's feeder (P2) where an operator
  cueing a song ("Play Next"), firing a station ID/ad, or editing the live
  rotation at the same moment the system was pulling fresh songs from the NAS
  could silently vanish: the cued item would be dropped from bookkeeping and
  its just-copied local file deleted while it was still queued to play. The
  song would just never air, with nothing showing an error. All feeder
  bookkeeping is now serialized so this can't happen. A newly-cached file
  also can't be deleted for at least 10 minutes no matter what, as a second
  safety net.
- A failed ad/station-ID insert (e.g. a brief network hiccup) no longer
  silently gives up that spot's time slot — it now retries for up to 10
  minutes before finally logging it as missed, so a real network blip
  doesn't quietly cost a sponsor their ad play.

### Changed
- The system now only queues a few songs ahead in the live player (was ~45
  minutes' worth) — the 45-minute local cache is unchanged and still fully
  protects against a NAS/network outage (the player's own emergency filler
  reads straight from that cache), but now the on-air queue reflects reality
  almost immediately instead of lagging up to 45 minutes behind edits.
  Configurable via `core.feed_ahead_tracks` in config.json (default 3).

## [1.0.0] - 2026-07-30

First public release. StudioFire has been on air 24/7 at KDPI since July 2026 —
everything below is running in production at a real FM station.

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
- Import your old ZaraRadio playlists: upload a .lst file on the Playlists
  page and it becomes a normal StudioFire playlist. Paths written on another
  computer (like \\KDPI-Media\music) are automatically translated to where
  the music lives on this machine.
- Big blinking ON AIR light at the top of the screen: glows red while audio is
  actually going out, goes dim to OFF AIR when paused or nothing is playing.
- Now Playing shows the real song name (not a cryptic cache filename) and the
  time: how far in, how long the song is, and how much is left.
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
- Reports page: pick a date range and see exactly what aired — everything, music
  only, or spots only (Station IDs / ads / PSAs, i.e. proof of performance) — with
  a one-click CSV export for affidavits/logs. Built from the as-aired play journal.
- Global library search on the On Air page: search your whole music library and
  drop any song in live with "Insert Next" — a one-off cue that plays right
  after the current song, without touching the saved rotation playlist.
- A big live clock in the top bar (every page) — radio runs on the wall clock.
- Studio health moved to a small colored badge at the top (next to Sign out).
  It's green when all is well, turns yellow or red if anything needs attention;
  click it to drop down the details (music library, disk space, index).
- Settings page (admin only): point StudioFire at your station folders —
  Shows, Advertisements, Station IDs, Jingles, PSAs — with a built-in folder
  browser, no typing paths. These will drive automatic scheduling next.
- EMERGENCY button on the On Air page: one press puts the emergency filler on
  air immediately and keeps it there — the automation will NOT sneak back in —
  until you press RESUME NORMAL. Survives restarts of the audio engine.
- Backup & restore on the Playlists page (admin only): download one file with
  every playlist in it; restore it later on this or another machine. Restoring
  never overwrites — same-named playlists come back as "(restored)" copies.
- Emergency audio no longer needs hand-picked filler files. If the emergency
  folder is empty, the engine plays real music from its local song cache
  instead — listeners hear normal songs, not a repeating clip. (Adding files
  to `assets\emergency\` still works and takes priority — useful for station
  IDs or "technical difficulties" messages.)
- A Windows installer (installer\StudioFire.iss + build_payload.py): one
  setup.exe that bundles Python, mpv, and NSSM — nothing to pre-install on a
  customer PC. The wizard asks for the station name and music folder, can
  register the auto-restarting Windows services, and opens the firewall for
  the web GUI. Upgrades keep the existing config and data.
- A ⟳ "restart everything" button next to the ON AIR light (and restart-all.bat)
  to manually cycle all services during testing. Guarded by a config flag
  (allow_gui_restart) so it can be turned off on the on-air PC.
- Now Playing and the History / Log now show the real Artist / Album / Song,
  read straight from each song's tags (via its cached copy) — no more cache-hash
  gibberish in the log, and track times are accurate too.
- "Fix broken file paths" on the Playlists page: many playlists (imported before
  the NAS move) point at old locations — this repoints each stale track to the
  real file in your library, matched by name. No re-import needed.
- Google Analytics (GA4) usage telemetry in the web GUI, so we can see which
  features stations actually use. The station name is attached to the data.
  It never affects playback and the GUI works identically with no internet.
  A station can opt out (or use its own GA property) with
  `"core": {"ga_measurement_id": ""}` in config.json. See "Telemetry & privacy"
  in the README.
- Small fixes from first hands-on use: pressing Enter now creates the
  playlist, and empty inputs tell you what to do instead of doing nothing.
- Project scaffold: four-service layout (engine / core / worker / poller), config schema,
  logging locations, and planning docs (PLAN.md v0.3).

### Changed
- Playlists now work exactly like ZaraRadio: every playlist IS a .lst file on
  disk. "Open a playlist" is a file explorer — browse to any .lst (including
  your old Zara ones) and open it; every change you make saves straight back
  to that same file, so Zara and StudioFire can share the very same playlists.
  If a file was edited outside StudioFire, opening it picks up those changes.
  Deleting a playlist deletes its file. New playlists are saved into the
  "Playlists folder" (Settings). The editor shows which file it's saving to.
- The Playlists page is now just two simple actions: open a playlist (file
  explorer) or start a new one. Duplicate, Delete, and PUT ON AIR live inside
  the playlist editor, where you can see exactly what you're acting on. The
  full playlist export/restore moved to Settings (admins only).
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

### Fixed
- Intermittent errors on every page under load — the database connection wasn't
  safe to hand between the web server's worker threads. Fixed.
- A rare file-lock error while caching songs ahead (which also quietly skipped a
  feed cycle now and then) is gone.
