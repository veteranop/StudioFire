---
tags: [reference, studiofire]
status: active
created: 2026-07-15
up: "[[PROJECTS-INDEX]]"
---


[[01-Active-Revenue]]

# StudioFire — Deploy & Dry-Run Guide

StudioFire is a **web app**. Only the machine that *runs* it needs Python; DJs
just open a browser to it. The engine (P1) and web (P2) talk over
`localhost:7701`, so **all services run on one machine**, and that machine
should be **local to the NAS** for fast playback.

---

## What travels where

| Piece | GitHub | `\\KDPI-Media\music\StudioFire` (deploy kit) | Created on the box |
|-------|:------:|:--------------------------------------------:|:------------------:|
| Code (`services/`, `web/`, tests) | ✅ | ✅ (mirror) | |
| `config/config.example.json`, `requirements.txt` | ✅ | ✅ | |
| `bin\mpv.exe` (~117 MB) | ❌ gitignored | ✅ put it here | |
| `config\config.json` (real config) | ❌ gitignored | ✅ a template here | edit per box |
| `assets\emergency\*.mp3` (filler) | ❌ gitignored | ✅ optional | |
| `data\`, `precache\`, `logs\` | ❌ | ❌ (keep LOCAL) | ✅ auto |

**Never point `precache`/`data`/`logs` at the NAS** — they must be on the box's
local disk.

---

## First-time setup on a fresh box (dry-run PC)

1. **Anaconda** (Python 3.11+). Same as dev for consistency. Have `python` on PATH.
2. **Use a UNC NAS root if possible:** set `paths.nas_music_root` in
   `config.json` to `//KDPI-Media/music/G`. Only use a mapped drive like `Z:`
   if you need compatibility with legacy playlists that still contain
   `Z:\G\...` paths.
3. **Get the code** — either:
   - `git clone https://github.com/veteranop/StudioFire` , **or**
   - copy `\\KDPI-Media\music\StudioFire` to a local folder.
4. **Install deps:** `pip install -r requirements.txt`
5. **mpv:** copy `bin\mpv.exe` from the deploy kit into `bin\`.
6. **Config:** `copy config\config.example.json config\config.json` and edit —
   `station_name`, `paths.nas_music_root` (`//KDPI-Media/music/G`), leave
   `paths.path_aliases` empty if you are using UNC paths, and set ports.
7. **(Optional)** drop a couple of `.mp3` filler files (station IDs / sweepers)
   in `assets\emergency\`. If you skip this, the engine uses cached rotation
   music from `precache\` as its emergency audio — listeners hear real songs.
8. **Run:** `start-all.bat` (three console windows open). Stop with
   `stop-all.bat`.
9. **Open** `http://<this-box>:8080` — first visit creates the admin account.
   From home, VPN in and hit the same URL.

---

## Dev → deploy workflow (what you asked for)

Work locally at home, then publish to **two** places:

**1. GitHub (source of truth / history):**
```
git add -A && git commit -m "..."
git push origin main
```

**2. `\\KDPI-Media\music\StudioFire` (deploy mirror):** keep it a *complete,
runnable* copy — code **plus** the gitignored runtime bits (`bin\mpv.exe`,
`config` template, `assets\emergency`), but **not** `data\ precache\ logs\`.
Use robocopy (adjust the source path):
```
robocopy "C:\Users\<you>\Desktop\Projects\StudioFire" "\\KDPI-Media\music\StudioFire" /MIR /XD .git data precache logs installer .playwright-mcp __pycache__ /XF *.db *.db-wal *.db-shm queue_state.json heartbeat.txt
```

**On the dry-run / on-air box, to update:** see "Updating a station" below.
A developer `git clone` is the exception: use `git pull`, then restart.

---

## Publishing a patch (GitHub is the source of truth)

Every installed station updates itself from **GitHub Releases** on
`veteranop/StudioFire` (a public repo, so stations need no login). A
station only ever sees **published releases**. Pushing to `main` alone
changes nothing on air.

1. Write the operator-facing notes under `## [Unreleased]` in
   `CHANGELOG.md`, in plain English. Operators read them before they press
   Install.
2. Merge to `main` and push.
3. Run: `python scripts/release.py 1.1.0 --dry-run`, check the notes, then
   run it again without `--dry-run`.
   The script checks that `main` is clean and in sync and that the version
   is newer. It then runs the whole test suite, turns `[Unreleased]` into
   `[1.1.0] - <date>`, writes `VERSION`, commits, tags `v1.1.0`, pushes,
   and publishes the GitHub release (it needs the `gh` CLI, logged in).

The `VERSION` file inside a tagged release **must** match its tag. The
updater refuses a release where they differ, so always release with the
script. (Note: `v1.0.1` was tagged by hand with `VERSION` still at 1.0.0,
so stations will correctly refuse it. The next release made with the script
supersedes it.)

**Patches that need a new Python package:** if `requirements.txt` changes,
the updater refuses the release on purpose. It can't safely install packages
into the bundled runtime while services are running. Ship those as a new
installer build instead (see `installer/README.md`). The same applies to a
new `mpv.exe` or NSSM.

---

## Updating a station

**From the web GUI (normal way):** Settings → **Software updates**.
StudioFire checks GitHub every 6 hours, and a green **⬆ Update** pill
appears on the On Air page when there's a newer release. An **admin**
presses **Install update**. Nothing ever installs by itself.

**From the box:** Start menu → *Update StudioFire from GitHub*, or run
`update.bat` in the install folder (`update.bat check` only reports).

What an update does (`services/updater.py`, log in `logs\update.log`):
1. Downloads the release and verifies it **before touching anything**: the
   VERSION matches the tag, every file compiles, and there are no new Python
   packages.
2. Backs up the current code **and the database** to `data\updates\backup-*`
   (the last 3 are kept).
3. Swaps in the new code. `config\config.json`, `data\`, `logs\`,
   `precache\`, `assets\`, `bin\` and `runtime\` are never touched.
4. Restarts **only what changed**. Most patches restart only the web GUI and
   the library indexer, and **the music keeps playing**. The audio engine
   restarts only when engine code changed, and then the audio drops for a
   few seconds. The GUI warns about this, so pick a quiet moment, not
   during an ad or a live show.
5. Checks that everything came back on the new version. If not, it puts the
   old version back by itself and reports **"rolled back"**. A release
   that crashes on startup is detected within about 15 seconds.

**To go back to an older release by hand:**
`runtime\python.exe -m services.updater apply --tag v1.0.5 --force`

**Requirements on the box:**
- Internet access to `api.github.com` / `codeload.github.com`.
- The account the services run as must be able to modify the install
  folder. Installers from this version on grant that (`users-modify`).
  On an older install, grant *Modify* on the StudioFire folder to that
  account once, or install the new build over it.

**Bootstrapping:** stations on 1.0.x have no updater yet. They need **one**
manual upgrade: install the new installer over the top (config is kept).
After that, every patch comes from GitHub.

**Testing / private mirror:** set `"update_api"` at the top level of
`config\config.json` (or the env var `STUDIOFIRE_UPDATE_API`) to another
releases API base URL.

---

## Production (on-air PC) — later

Same layout, but run each service as an **auto-restarting Windows service via
NSSM** (so a crash or reboot self-heals; P1 must always come back). With
`bin\nssm.exe` in place, run as Administrator:
```
scripts\install-services.bat
```
It registers all three (auto-start, restart-on-crash, rotated service logs)
with the working directory set correctly. `scripts\remove-services.bat` undoes
it.

**Then do BOTH of these, or the services sit "Paused" (learned 2026-07-08):**
1. **Log On as a real user** — services.msc → each StudioFire\* service →
   Log On → *This account* (the box's username/password). LocalSystem has no
   usable Python profile and no NAS credentials.
2. **Map the NAS inside the service session** —
   `copy config\drive-map.example.bat config\drive-map.bat` and edit the UNC.
   Services never inherit your logged-in drive letters; the wrapper
   (`scripts\svc-run.bat`) runs this before each service starts. **Easier still:** build the customer installer — see
[[StudioFire/installer/README|installer/README]] — which bundles Python, mpv,
NSSM, and a station-setup wizard into one setup.exe.

---

## After deploying: fix the legacy playlists

The pre-Synology playlists point at old paths. Once the library has finished
indexing (**Studio health → Library index**), go to **Playlists → 🔧 Fix broken
file paths** to repoint every moved track to its real file in `Z:\G`.

## Related
- [[PROJECTS-INDEX]]
