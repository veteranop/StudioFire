---
tags: [reference, studiofire]
status: active
created: 2026-07-15
up: "[[PROJECTS-INDEX]]"
---


[[01-Active-Revenue]]

# StudioFire — Building the Windows Installer

Produces a single `StudioFire-Setup-<version>.exe` a customer double-clicks.
No Python, no Anaconda, no PATH edits on their box — the installer bundles an
embedded Python runtime with all dependencies pre-installed.

## What the customer gets

- One wizard: install folder, **station name**, **music library path** → written
  to `config\config.json` (upgrades keep the existing config, no re-ask).
- Optional task: register the three services with NSSM (auto-start at boot,
  auto-restart on crash) — check it on the production on-air PC. **After
  installing**, set each service's Log On to a real user account. If the music
  is on a NAS, use a UNC root in `config\config.json` like
  `//KDPI-Media/music/G`; only create `config\drive-map.bat` when you must
  support legacy mapped-drive paths in a service session.
  The bundled `runtime\` means Python itself works under any account.
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

## Code signing (SmartScreen)

An unsigned `StudioFire-Setup-<version>.exe` shows **"Windows protected your PC"
/ Unknown publisher** on a customer's first run — the single biggest trust hit on
an on-air install. Sign it with an OV or EV Authenticode certificate (an EV cert
builds SmartScreen reputation immediately; an OV cert accrues it over time).

Signing is **opt-in** in `StudioFire.iss` so a dev box with no certificate still
builds. Set `STUDIOFIRE_SIGN_TOOL` and define the matching SignTool for ISCC
(`$f` is substituted with each file to sign):

```bat
set STUDIOFIRE_SIGN_TOOL=studiofire
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" ^
  /Sstudiofire="signtool.exe sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /f C:\keys\veteranop-codesign.pfx /p %CERT_PW% $f" ^
  installer\StudioFire.iss
```

With `STUDIOFIRE_SIGN_TOOL` unset, no `SignTool`/`SignedUninstaller` directives
are emitted and the build is byte-for-byte the unsigned one.

Verify what you shipped:

```bat
signtool verify /pa /v installer\Output\StudioFire-Setup-<version>.exe
```

Keep the `.pfx` (and its password) **out of the repo** — it is a private key. It
belongs in the password manager / a CI secret, never in `installer\`.

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
