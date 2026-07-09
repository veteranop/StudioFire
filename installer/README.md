[[01-Active-Revenue]]

# StudioFire — Building the Windows Installer

Produces a single `StudioFire-Setup-<version>.exe` a customer double-clicks.
No Python, no Anaconda, no PATH edits on their box — the installer bundles an
embedded Python runtime with all dependencies pre-installed.

## What the customer gets

- One wizard: install folder, **station name**, **music library path** → written
  to `config\config.json` (upgrades keep the existing config, no re-ask).
- Optional task: register the three services with NSSM (auto-start at boot,
  auto-restart on crash) — check it on the production on-air PC.
- Optional task: open firewall port 8080 for the LAN web GUI.
- Start-menu entries: Start / Stop / Health check / Web GUI.
- **No emergency filler audio is shipped.** The engine uses cached rotation
  music from `precache\` as its emergency tier (PLAN §10.1) — listeners hear
  real songs during a failover, not a canned clip. Stations can still drop
  IDs/sweepers into `assets\emergency\` to take priority.
- Uninstall stops/removes the services but **leaves config, data, and logs**.

## Build steps (your dev box, internet required)

1. **One-time:** install [Inno Setup 6](https://jrsoftware.org/isdl.php).
2. Make sure your known-good `bin\mpv.exe` is in the repo (it's gitignored).
3. Prepare the payload (embedded Python 3.12 + pip deps + NSSM):
   ```
   python installer\build_payload.py
   ```
4. Compile:
   ```
   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\StudioFire.iss
   ```
5. Ship `installer\Output\StudioFire-Setup-<version>.exe`.

`installer\payload\` and `installer\Output\` are gitignored build artifacts;
re-run step 3 only when Python/deps need refreshing.

## How the bundled runtime works

`build_payload.py` patches the embedded distribution's `python312._pth` to add
`..` (the install root) to `sys.path`, so `python -m services.engine.main`
resolves from anywhere with no cwd or PATH assumptions. `start-all.bat`,
`healthcheck.bat`, and `scripts\install-services.bat` all prefer
`<install>\runtime\python.exe` when it exists and fall back to Anaconda/PATH
on dev boxes.

## Related

- [[StudioFire/DEPLOY|DEPLOY]] — manual deploy / dev→deploy workflow
- [[StudioFire/docs/Operations|Operations]] — running & soak testing
