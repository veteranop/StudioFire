# StudioFire — On-Air PC Deployment & Auto-Start Runbook

> **This file SUPERSEDES the `ON-AIR-SETUP.md` that shipped inside the
> `StudioFire-OnAir-v1.0.0` kit.** The kit's copy tells you to run the station as
> NSSM Windows services and calls console mode "fine for testing, not what you
> want for real on-air". **That guidance is reversed and is wrong for any box that
> must make sound** — see the errata below. Replace the kit's copy with this one
> when the on-air box is next touched in a planned window.

---

## ERRATA — why the old `ON-AIR-SETUP.md` was wrong (2026-09-30)

The old runbook's Hop 3/Hop 4 registered `StudioFireEngine/Web/Worker` as NSSM
services with auto-start at boot, and described the console-mode fallback
(`start-all.bat`, three windows) as "fine for testing, not what you want for real
on-air". **The opposite is true.**

**A Windows service runs in session 0, which has no audio endpoint.** Audio devices
are session-scoped, so a service-hosted engine starts, launches mpv, and then fails
to initialise its audio output for *every* track:

```
ERROR engine.supervisor: decode/play error on deck A
  C:\StudioFire\precache\*.mp3: audio output initialization failed
ERROR engine.supervisor: ENTERING EMERGENCY MODE: track ended
```

Proven live on `.200` on 2026-09-29: services installed 19:59:03, and by 20:00:28 the
engine had logged that on **3,529 tracks and climbing** — while `GET /health` returned
`{"ok":true,...,"version":"1.3.2"}` and `services.msc` showed every service **Running**.
Process health and SCM state can both be green on a silent station. mpv itself launched
fine (pids present); it was the audio *output* that could not open.

There is **no** Microsoft-supported way to give a normal WASAPI app an audio endpoint
in session 0 (UI0Detect was removed from modern Windows). This is a platform limit, not
a StudioFire bug. The full evidence and the Rosie+Elon consensus (8.7/8.7) are in
`docs/AUTOSTART-CONSENSUS.md`.

**Consequences:**
- `scripts\install-services.bat` now **refuses to run** unless you pass
  `-i-understand-no-audio`, and points you at the right tool.
- The production auto-start for an audio host is **autologon + an at-logon task**
  (`scripts\install-autostart.bat`), which runs the stack in a real interactive
  session where audio works — and still comes back by itself after a power bump.
- Any box you converted to services must be taken **out** of service mode before it
  is trusted: `scripts\remove-services.bat`.

---

## What this runbook is for

Getting StudioFire onto a fresh on-air PC and configured so that **after a power
bump or Windows restart it comes back on its own, WITH SOUND, and nobody touches the
keyboard.**

The kit is self-contained: it carries its own Python (`runtime\`) plus `bin\mpv.exe`
and `bin\nssm.exe`. No Anaconda, no pip, no internet needed on the target.

---

## The signal chain (why session matters)

```
NAS → precache (local disk) → mpv engine → sound card → Barix Instreamer → STL → transmitter
```

Every arrow after the engine is **session-scoped audio**. If the engine is not in a
logged-in interactive session, the chain breaks at "sound card" and nothing tells you
except the log.

---

## Before you start — 3 things to confirm

| # | Thing | Why |
|---|-------|-----|
| 1 | Which NAS share you're staging on | `\\KDPI-Media\music\StudioFire` (canonical) or `\\192.168.6.200\StudioFire` (the `Y:` staging share) |
| 2 | **The on-air PC's local admin username + password** | Needed for autologon (stored as an LSA secret) — the box logs itself in with it |
| 3 | Where on the on-air PC it should live | A **local disk** (`C:\StudioFire`). Never run it from the NAS — playback must not depend on the network |

---

## Hop 1 — your PC -> NAS

Fastest is the single ZIP (one file, far less likely to land half-copied over SMB):

```cmd
copy "C:\Users\markd\Desktop\StudioFire-OnAir-v1.0.0.zip" "\\KDPI-Media\music\"
REM then on the on-air PC:
copy "\\KDPI-Media\music\StudioFire-OnAir-v1.0.0.zip" "C:\"
powershell -Command "Expand-Archive -Path C:\StudioFire-OnAir-v1.0.0.zip -DestinationPath C:\StudioFire -Force"
```

Verify the ZIP survived: its SHA256 must match the sidecar `...zip.sha256`
(`certutil -hashfile ... SHA256`).

Or copy the folder directly (slower — ~3,694 files). Use `/E` (merge), **not `/MIR`**,
unless the destination holds nothing else you care about:

```cmd
robocopy "C:\Users\markd\Desktop\StudioFire-OnAir-Ready" "\\KDPI-Media\music\StudioFire" /E
```

Expect ~217 MB on the share (dominated by `bin\mpv.exe` at ~112 MB and `runtime\`).

---

## Hop 2 — NAS -> on-air PC

```cmd
robocopy "\\KDPI-Media\music\StudioFire" "C:\StudioFire" /MIR
```

Confirm the critical pieces landed — all three must exist:

```cmd
dir C:\StudioFire\runtime\python.exe
dir C:\StudioFire\bin\mpv.exe
dir C:\StudioFire\config\config.json
```

If `runtime\python.exe` is missing the copy was incomplete — re-copy the whole folder.

---

## Hop 3 — configure, then turn on auto-start

### 3a. Config — set the audio output explicitly

`config\config.json` ships with `"audio_device_guid": ""`, which means "the Windows
default device". A default device is per-session and moves around (a monitor wakes up,
Bluetooth grabs the endpoint, Windows renumbers outputs), so on a box feeding a fixed
device (the sound card wired to the Barix) **lock the engine to that device**.

```cmd
REM list what mpv can see, then pick the output that feeds the Barix
scripts\set-audio-device.bat -List
scripts\set-audio-device.bat -Value "wasapi/{GUID}"
scripts\set-audio-device.bat            REM interactive picker
scripts\set-audio-device.bat -Check     REM what is set now
```

Restart the stack for the change to take effect. (This is hygiene for the interactive
session. It does **not** make a service able to play audio — nothing does.)

### 3b. Auto-start that keeps audio — the one command

Right-click **`scripts\install-autostart.bat`** -> **Run as administrator**. It:

- refuses if the session-0 services are still installed (remove them first with
  `scripts\remove-services.bat`) — services and console mode must not coexist
- turns on Windows autologon for the station account, storing the password as an
  **LSA secret** (`scripts\autologon-lsa.ps1`) — not cleartext in the registry
- creates two Task Scheduler tasks: **StudioFire-Autostart** (At log on, *run only
  when the user is logged on*, 45 s delay -> `start-all.bat`) and
  **StudioFire-Watchdog** (every 5 min -> `scripts\watchdog.bat`)
- verifies what it created and prints the autologon state

Preview with `scripts\install-autostart.bat -check` (changes nothing).
Then it starts nothing else — the task fires at the *next* logon, which is what the
reboot test below proves.

Undo: `scripts\remove-autostart.bat`.

### 3c. BIOS

Set **"Restore on AC power loss" = On**, or a power cut leaves the machine sitting off
and none of the above matters.

---

## Hop 4 — GO LIVE and prove it

Start the station by hand once (in the logged-in session):

```cmd
start-all.bat
```

- Web GUI: `http://localhost:8080` — first visit creates the admin account
- LAN GUI: `http://<on-air-pc>:8080` — what DJs use
- Health: `healthcheck.bat` — says ON AIR vs. stuck on emergency filler
- Stop: `stop-all.bat`

Then the **only acceptance test that counts**:

1. Confirm the station is playing real audio right now.
2. **Reboot the box and do NOT touch the keyboard.**
3. Confirm it comes back by itself, **with audio**, and `healthcheck.bat` reports ON AIR.

A box can report healthy and be silent — that is exactly what tricked us on `.200`.
**Listen to the stream / check the board.** HTTP 200 is not proof of on-air.

---

## Known gotchas

- **Never run from the NAS.** `data\`, `precache\`, `logs\` must stay on the local disk.
- **`precache` must be local** — it is the tier-1 failover for playback.
- **Services never inherit your mapped drives**, and neither does a boot autostart.
  Prefer UNC paths in config and stored playlists over drive letters. Since v1.2.0 the
  resolver re-points a stored `Z:\...` at the configured NAS root when `Z:` is absent,
  but UNC is still the right thing to store.
- **Do not convert an audio host to Windows services.** See the errata at the top.
- **An interactive logon as a *different* user rewrites `DefaultUserName`** (Windows
  tracks the last logged-on user) and breaks autologon. On a locked-down kiosk this
  does not happen; if it ever does, re-run `install-autostart.bat`.
- **Windows Firewall** — the LAN GUI needs inbound TCP 8080 open on the on-air PC.
- **After the library indexes**, fix legacy playlists via Playlists -> "Fix broken file paths".
- **A watchdog on the box that watches itself is a diary, not a watchdog.** The
  off-box, silence-aware watchdog is specified in `docs/AUTOSTART-WATCHDOG.md`.

---

## Rollback

- Stop: `stop-all.bat`
- Remove auto-start: `scripts\remove-autostart.bat` (config, data and logs preserved)
- Remove services (if any were installed): `scripts\remove-services.bat`
- Full removal: delete `C:\StudioFire`. Nothing else on the box was modified.

---

## What's in the kit

| Piece | Included | Note |
|-------|:--------:|------|
| `services\`, `web\`, `scripts\`, `tests\` | yes | application code |
| `runtime\` (embedded Python + all deps) | yes | why the target needs nothing installed |
| `bin\mpv.exe` / `bin\mpv.com` / `bin\nssm.exe` | yes | audio engine (console build is `mpv.com`) |
| `config\config.json` | yes | pre-filled per box — edit `audio_device_guid` |
| `start-all.bat`, `stop-all.bat`, `healthcheck.bat` | yes | console-mode control |
| `scripts\install-autostart.bat`, `watchdog.bat` | yes | the auto-start path (this doc) |
| `data\`, `precache\`, `logs\` | no | created locally on first run |
| `.git\`, `installer\` | no | dev-only, excluded |

Companion docs: `docs\AUTOSTART-CONSENSUS.md` (the decision and evidence),
`docs\AUTOSTART-WATCHDOG.md` (off-box monitoring), `DEPLOY.md`, `docs\Operations.md`.
