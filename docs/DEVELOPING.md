# Developing StudioFire

Everything here is for people working on the code. If you just want to run a
station, use the installer — see the [README](../README.md).

## Requirements

* Windows 10/11 (the app targets Windows; the engine drives `mpv.exe`)
* Python 3.12
* `mpv.exe` in `bin\` (the release payload uses the same copy)
* A music folder — a local path or a UNC share. No mapped drive is required.

## Run from source

```
pip install -r requirements.txt
start-all.bat          # engine + core/web + indexer
healthcheck.bat        # is it on air?
```

Web GUI: `http://127.0.0.1:8080` — a fresh checkout with no
`config/config.json` goes straight to the setup wizard. That is the same path a
new station takes, so it is the best way to test the first-run experience.

Individual pieces:

```
python -m services.core.main        # web UI + API
python -m services.worker.main      # library indexer
python -m services.engine.supervisor  # audio engine supervisor
```

## Tests

```
for %f in (tests\test_*.py) do python %f
```

Or one at a time: `python tests\test_reporting.py`. Every test is self-contained
(temp DB, temp folders, no network, no on-air hardware). The full suite must be
green before a release — `scripts\release.py` refuses to publish otherwise.

Two rules that matter more than style:

1. **Playout never changes.** The audio engine, its fallback chain and the
   precache digest (`engine_bridge.Precache.cache_path_for`, mirrored by
   `sx_report.precache_digest`) are load-bearing for every historical play. A
   test asserts the two digests agree.
2. **Every station-specific value comes from config.** No hostnames, IPs, drive
   letters or names in tracked files; see `config/CONFIG.md`.

## Building the installer

```
python installer\build_payload.py                 # embedded Python + deps + NSSM
"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\StudioFire.iss
```

Output: `installer\Output\StudioFire-Setup-<version>.exe`, which is gitignored.
`installer\README.md` covers code signing and payload contents.

Quick check that the payload is sane:

```
powershell -ExecutionPolicy Bypass -File scripts\ci-install-smoke.ps1
```

The full clean-VM acceptance run is in
[docs/INSTALL-ACCEPTANCE.md](INSTALL-ACCEPTANCE.md).

## Cutting a release

```
python scripts\release.py 1.7.0
```

It moves `[Unreleased]` in `CHANGELOG.md` into a versioned section, bumps
`VERSION`, runs the tests, builds the installer, commits, tags, pushes, creates
the GitHub release **with the installer attached**, and publishes. If the
installer build fails or the exe is missing, the release fails — a release
without an installer is not shippable.

Write the release notes for an operator, not a developer: what changed, what
they will notice, and anything they must do.

## Running a branch against real station data

A `git checkout` on the on-air or production PC is running unreleased code
against a live library and a live database. Before you start the app:

```powershell
copy data\studiofire.db "data\backups\pre-branch-$(Get-Date -Format yyyyMMdd-HHmmss).db"
```

The app also takes its own snapshot: **every schema migration copies the database
first** to `data\backups\pre-migration-v<old>-to-v<new>-<stamp>.db`, verifies the
copy with `PRAGMA integrity_check`, and refuses to migrate if either step fails
(`services/core/db.py`). The last five snapshots are kept. That covers the app's
own startup path — it does **not** cover a migration you run by hand, a script
that writes, or restoring after a bad branch. Make your own dated copy anyway,
and put it outside the repo if the branch might move files around.

To roll back a schema upgrade: stop both services, replace `data\studiofire.db`
with the snapshot, `git checkout` the previous tag, restart. Older code will
refuse to run against a newer schema — that is deliberate.

## Where things live

| Path | What |
|---|---|
| `services/engine/` | audio engine, supervisor, queue store — the playout path |
| `services/core/` | FastAPI app, scheduler, spots, reports, setup wizard |
| `services/worker/` | library indexer, tag reads |
| `web/` | Jinja templates, static CSS/JS, icons |
| `scripts/` | operator + dev scripts (release, backup, smoke tests) |
| `installer/` | Inno Setup script and payload builder |
| `site-notes/` | **gitignored**, per-installation private notes — never commit |

## House rules

* Keep the UI honest: if something is unknown or missing, say so on screen
  rather than filling it with a plausible value.
* No browser `alert()`/`confirm()` in the operator UI — use the in-app dialog.
* No emoji as icons; inline SVG only.
* Never log or display a stream password, and never write a secret into the
  database or config.
