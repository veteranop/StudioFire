---
tags: [reference, studiofire]
status: active
created: 2026-10-05
up: "[[PROJECTS-INDEX]]"
---


[[01-Active-Revenue]]

# Release & Branches

How StudioFire code becomes a thing a station runs, and the branch model that
keeps a half-built 2.0 away from live stations. Deploy mechanics are in
[[StudioFire/DEPLOY|DEPLOY]]; the 2.0 program is in
[[StudioFire/docs/2.0-Design-Decisions|2.0 Design Decisions]].

## The one rule

**A station only ever sees a published GitHub Release, and releases are cut from
`main` only.** Pushing to `main`, or to any other branch, changes nothing on a
station by itself. The updater (`services/updater.py`) installs published
releases on `veteranop/StudioFire`; `scripts/release.py` is the only supported
way to publish one, and it refuses to run anywhere but `main`.

## Branch model

| Branch | Role | Releases from it? |
|---|---|---|
| `main` | The **1.x production line.** 1.x hotfixes release from here. | **Yes** — every release. |
| `v2-dev` | The long-lived **2.0 build.** Branched off v1.5.0 (`5627321`) on 2026-10-05. | **No** — never a release source until the major-version gate has shipped and been confirmed. |
| `planning/2.0` | The 2.0 **planning spec** (held the 2.0 doc before `v2-dev` existed). | No. |

Rules:
- All 2.0 work happens on `v2-dev`. 1.x hotfixes still release from `main`.
- **Do not merge `v2-dev` → `main`** until the major-version gate (below) has
  shipped to stations in a 1.x release and been confirmed installed. Merging
  early would make the next 1.x hotfix release carry 2.0 code.
- Merging the other direction — **`main` → `v2-dev`** — is fine and expected, to
  keep the 2.0 branch current with 1.x hotfixes.
- `main` is branch-protected against force-push and deletion (2026-10-05).
- Never run `scripts/release.py` with a 2.x version until the gate and the
  pre-migration backup are complete.

## The release process (from `main`)

Run `scripts/release.py` (a developer tool; runs only in a git checkout). It
stops at the first problem **before changing anything**:

1. On `main`, clean working tree, in sync with `origin/main`.
2. The new version is newer than `VERSION` and every existing tag.
3. `CHANGELOG.md` has entries under `## [Unreleased]` — these become the release
   notes operators read before they press Install. Write them in plain English.
4. Runs the whole test suite (`tests/test_*.py`) unless `--no-tests`.
5. Renames `[Unreleased]` → `[x.y.z] - <date>`, writes `VERSION`, commits
   `Release vx.y.z`, tags, pushes both, and publishes the GitHub release via the
   `gh` CLI.

`python scripts/release.py x.y.z --dry-run` shows what it would do and changes
nothing. The **`VERSION` in the tagged commit must equal the tag** — the updater
refuses a release where they differ, so always release with the script. Per the
DEPLOY.md checklist (on `main`), a release is **not done until the Operator's
Guide reflects any operator-visible change** — content plus the cover
version/date.

## The MAJOR-version guard (two layers)

A major bump (1.x → 2.x) is dangerous because the **installed 1.x updater has no
notion of major versions**: a published 2.0.0 would be offered to every 1.x box
behind the same everyday "Install update" button. Two defenses:

### Layer 1 — publish-side guard (in code on `main`)

`scripts/release.py` **refuses a MAJOR-version bump without an explicit `--major`
flag** (commit `f953acb`, 2026-10-05). It compares the new major to the current
`VERSION`'s major and fails with a message pointing at the gate. So a stray
`2.0.x` cannot be published by accident, and the everyday in-app update button
can never be handed a new major. **Do not pass `--major` until Layer 2 is
confirmed installed at every station.**

### Layer 2 — the updater gate (ships FIRST, in a 1.x release — NOT YET BUILT)

This is step 0 of the 2.0.0 MVP spine. It must be on the stations *before* 2.0 is
published, because the installed 1.x code is what decides what to offer. When
built (target e.g. v1.6.0, from `main`):

- The updater lists releases (`/releases`, not just `/releases/latest`) and
  returns both `update_available` (newest **within** the current major — feeds
  the everyday button, unchanged) and `major_available` (newest with a **higher**
  major — never feeds the everyday button).
- `apply()` without `--tag` never crosses a major; `apply --tag v2.x` also
  requires a new `--major` flag (defense in depth).
- The Settings UI shows a separate **amber/red "Major Version Update" card**,
  **admin-only**, that requires the admin to **type the version** and a final
  in-page confirm, behind a pre-flight (free disk for backups, `PRAGMA
  integrity_check`, engine not in emergency, not within 10 min of a show). Nothing
  major ever auto-installs.

> **Branch note (2026-10-05):** Layer 1 was committed on **`main`** (commits
> `f953acb` and `24b0ba8`, which also updated `DEPLOY.md`), then merged
> `main` → `v2-dev`, so **both branches now carry the guard**. Layer 2 (the
> updater gate) is not built on any branch yet — it is step 0 of the 2.0.0 MVP
> spine, to ship in a 1.x release from `main` and be confirmed installed before
> any 2.0 release exists.

## What an update touches (and what it never does)

The updater only replaces the code the installer ships (`services/`, `web/`,
top-level `scripts/*.py|*.bat`, the root `.bat` files, `VERSION`, `CHANGELOG.md`,
`requirements.txt`, `config/config.example.json`). It never touches
`config/config.json`, `data/` (the DB), `logs/`, `precache/`, `assets/`, `bin/`
(mpv, nssm) or `runtime/` (bundled Python). A release whose `requirements.txt`
changed is refused on purpose — new packages need the full installer. It backs up
the code **and the DB** (online SQLite backup) before swapping, restarts only
what changed (the audio engine only when engine code changed), health-checks, and
**auto-rolls-back** on any failure. Full flow: [[StudioFire/DEPLOY|DEPLOY]].

## Related
- [[StudioFire/DEPLOY|DEPLOY]]
- [[StudioFire/docs/2.0-Design-Decisions|2.0 Design Decisions]]
- [[StudioFire/docs/Operations|Operations]]
- [[PROJECTS-INDEX]]
