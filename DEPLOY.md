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
2. **Map the NAS** the same way playlists expect it: `Z:` → `\\KDPI-Media\music`
   (so `Z:\G\...` resolves). If you must use a different letter/UNC, set it in
   `config.json` instead (see below).
3. **Get the code** — either:
   - `git clone https://github.com/veteranop/StudioFire` , **or**
   - copy `\\KDPI-Media\music\StudioFire` to a local folder.
4. **Install deps:** `pip install -r requirements.txt`
5. **mpv:** copy `bin\mpv.exe` from the deploy kit into `bin\`.
6. **Config:** `copy config\config.example.json config\config.json` and edit —
   `station_name`, `paths.nas_music_root` (`Z:/G`), `paths.path_aliases`
   (`{"\\\\KDPI-Media\\music": "Z:"}`), ports.
7. **(Optional)** drop a couple of `.mp3` filler files in `assets\emergency\`.
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
robocopy "C:\Users\<you>\Desktop\StudioFire" "\\KDPI-Media\music\StudioFire" /MIR /XD .git data precache logs .playwright-mcp __pycache__ /XF *.db *.db-wal *.db-shm queue_state.json heartbeat.txt
```

**On the dry-run / on-air box, to update:**
- If it was `git clone`d:  `git pull` then re-run `start-all.bat`.
- If it runs from the NAS mirror: re-copy locally (don't run over the network),
  then restart.

---

## Production (on-air PC) — later

Same layout, but run each service as an **auto-restarting Windows service via
NSSM** (so a crash or reboot self-heals; P1 must always come back):
```
nssm install StudioFireEngine  "C:\...\python.exe" "-m services.engine.main C:\StudioFire\config\config.json"
nssm install StudioFireWeb     "C:\...\python.exe" "-m services.core.main   C:\StudioFire\config\config.json"
nssm install StudioFireWorker  "C:\...\python.exe" "-m services.worker.main C:\StudioFire\config\config.json"
```
Set each to auto-start and set the working directory to the StudioFire folder.

---

## After deploying: fix the legacy playlists

The pre-Synology playlists point at old paths. Once the library has finished
indexing (**Studio health → Library index**), go to **Playlists → 🔧 Fix broken
file paths** to repoint every moved track to its real file in `Z:\G`.
