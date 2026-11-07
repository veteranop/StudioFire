# Install acceptance test — clean Windows VM

The bar: **a station manager goes from download to on air without opening a
terminal, installing Python, editing JSON or mapping a drive.** This is the run
that proves it. Do it on a clean Windows 10/11 VM with no Python, no mapped
drives and no earlier StudioFire.

Record the date, the build (`StudioFire-Setup-<ver>.exe` size + sha256) and the
result of each step.

## The seven steps

1. **Run the installer with defaults.**
   Double-click the setup exe → Welcome → Licence → install location
   (`C:\Program Files\StudioFire`) → leave “start automatically” and “desktop
   icons” ticked → Finish.
   *Expect:* no terminal, no Python prompt, no missing-dependency dialog. Accept
   the UAC prompt; that is the only one.
2. **Complete the wizard with a local music folder.**
   The Finish page opens the browser at `http://localhost:8080/setup`. Walk the
   nine screens: station name → admin account (choose the password — there is no
   default) → music folder (e.g. `D:\Music`) → playlists → skip spots → audio
   (play the test tone, answer honestly) → emergency → skip streaming → done.
   *Expect:* “Found N songs — indexing in the background”, then On Air.
3. **Hear audio.**
   Add a playlist (or use the auto-filled rotation) and press play.
   *Expect:* music through the chosen output device; the On Air page shows the
   track and a green health pill.
4. **Reboot, unattended.**
   Restart the VM and touch nothing.
   *Expect:* StudioFire is back on air with no login. With the service task
   ticked, that happens without autologon; autologon is only needed if you want
   the desktop app visible on a studio monitor.
5. **Repeat with a UNC share and no drive letter.**
   Point the music folder at `\\NAS\Music` (no `Z:` anywhere) and confirm playback
   and the playlist report work. This is the case the old installer failed.
6. **Upgrade in place.**
   Run the next version's setup exe over the top.
   *Expect:* station name, library path, playlists, users, history and the
   royalty-reporting settings all survive; services restart; the wizard does not
   reappear.
7. **Uninstall with “keep data”, then reinstall.**
   Uninstall → answer **Yes** to “Keep your settings, playlists and logs?” →
   reinstall → everything is restored, still no wizard.

## What can be automated (and is)

`scripts/ci-install-smoke.ps1` builds the payload, compiles the setup exe and
proves the bundled interpreter can import the app:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\ci-install-smoke.ps1
```

Full silent install/uninstall cycles need a disposable VM (they register
services, open a firewall port and write to `%ProgramData%`); run them there, not
on a station or a dev box that is also feeding audio.

Silent switches, for a VM run:

```
StudioFire-Setup-1.6.3.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
StudioFire-Setup-1.6.3.exe /VERYSILENT /SUPPRESSMSGBOXES /TASKS="services,firewall"
"%ProgramFiles%\StudioFire\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES
```

Then `curl http://127.0.0.1:8080/health` and `curl http://127.0.0.1:8080/api/setup/state`
before uninstalling.

## Known limits of the automated subset

* It does not start a real installed instance, because that means registering
  services and taking port 8080 on the machine running it.
* The nine-step wizard is covered by `tests/test_setup_wizard.py` (28 checks)
  against the real FastAPI app — fresh install → on air, including the
  “can’t see that folder” advice and the no-secrets guarantee on the settings
  copy.
* Code signing cannot be tested without a certificate; see `installer/README.md`.
