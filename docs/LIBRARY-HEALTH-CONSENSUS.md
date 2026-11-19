# Library health & missing files — LOCKED consensus (1.7.1)

Date: 2026-11-19 · Tickets: TimeTrax 1052 (this) · 996 (241-of-366 incident) · 1036 (1h41m of filler) ·
H2632301 (NAS recycle-bin recovery)
Inputs: `Rosie verdict` (8.7) + `Elon verdict` (8.8), both in
`Projects/.hermes/claude-handoff/studiofire-library-visibility-20261119-*.md`, plus live measurements
taken on the on-air box 2026-11-19 (read-only DB copy).

**Do not re-litigate this doc unless a hole appears. The build order below is the plan.**

---

## 1. Decision

Ship a **playlist-first, path-normalised health surface**: a Settings-style Playlists hub, a
missing-from-playlists report grouped by folder, a calm library-health card, and missing marks in the
playlist editor. **Do not** ship a whole-library missing browser, and **do not** rewrite playlist rows.

Why: the operator's question after the NAS reorg was "which of my playlists won't play, and what was in
them" — not "list 265,000 index paths". The index already knows more than the UI shows; the gap is
presentation, honest wording and path normalisation, not a new scanner.

## 2. Ground truth measured on `.222` (2026-11-19, read-only)

| Fact | Value |
|---|---|
| tracks / flagged missing / present | 358,987 / 264,945 (74%) / 94,042 |
| playlists / playlist items | 69 / 13,837 (all `item_type='file'`; folder types exist in schema, 0 rows) |
| playlist items flagged dead | **1,085 (8%)** → **712 distinct paths** |
| of those 712 (stat-checked on the real share) | **84 exist right now** (index stale) · **93 fix by path alias** · **~478 not found at any derivable path** · **~57 same-name candidates only (unverified)** |
| on-air rotation (`active_playlist_id = 59`, also `emergency_playlist_id`) | 259 items · **13 flagged · 12 genuinely gone · 1 index-stale** |
| runtime skips | 187 rows, reason `missing`; the last 12 are all `\\KDPI-Media\music\…` form |

Two traps this measurement exposed, both now non-negotiable:

1. **Path form.** `track.path` and `playlist_items.path` are mixed (`Z:/G\…`). Exact
   `t.path = i.path` joins report 6,480 of 6,982 as broken (93%); normalized joins report the truth.
   `playlists.py:165` (`item_durations`) still does the exact join.
2. **The alias is unset.** `path_aliases` is NULL and `_NAS_ROOT` empty, so `alias_path()` is a no-op:
   items stored as `\\KDPI-Media\music\…` (a name the box cannot resolve) are **skipped on air today**.
   The same file is reachable as `Z:\…`.
3. **Basename matching lies.** A same-name probe matched *Dread Zeppelin's* cover of "Black Dog" to
   *Led Zeppelin's*. No move claim may rest on a basename alone.

The dev DB (`StudioFire/data/studiofire.db`) is a copy with different debt (283,563 tracks / 188,914
flagged / 6,982 items / 3,436 dead). **Quote `.222` numbers to Mark; never quote dev numbers as the
station's health.**

## 3. What ships in 1.7.1 (build queue, in order)

1. **Path normalisation (code, no UI).** One helper — `normcase(normpath(...))` + slash fold — used by
   every health query; fix `item_durations` to use it; resolve `\\KDPI-Media\music` ≡ `Z:` through the
   existing `alias_path`/`path_aliases` mechanism (config, applied at feed time too).
   *Acceptance:* a mixed-form fixture joins correctly; alias resolves the stored form to the real file.
2. **Playlists page rebuilt as the Settings shell** — left `st-nav` (All playlists · Missing files ·
   Library health · Tools), right `st-main` + `st-card`. Detail editing stays at `/playlists/{id}` with
   a "← All playlists" link (do not stuff 1,800-row editors into a nav pane).
3. **All-playlists table** (local SQLite only): name · items · **unplayable** (missing + not-in-index) ·
   **% unplayable** · folder items broken · last updated · last aired (only if the query is cheap).
   **Sort:** pin the on-air/emergency playlist first, then unplayable count desc, then % desc, then name.
   Row click → the existing editor.
4. **"Missing from playlists" report** (branded PDF + CSV): banner = root reachability + as-of scan
   time; **folder groups** sorted by count with the complete folder path; under each group the full old
   file paths (Mark's ask); one "Probably moved to \<folder\> (N)" line only when the strong ladder
   below fires; CSV carries `status` ∈ {missing, probable_move, ambiguous, not_in_index} + candidate.
   Empty report = good day.
5. **Playlist editor**: header stats ("259 songs · 13 can't be found · ~18h") and dead rows marked
   muted + "Missing". Counts are distinct paths.
6. **Library health card + thin "Library index — snapshot" report**: reachability · last scan
   (`MAX(indexed_at)`) + newest/oldest mtime · present tracks / hours / size · missing *referenced by
   playlists* (headline) · index rows flagged missing (muted, secondary) · formats one-liner.
7. **Help menu + CHANGELOG + Operator's Guide** — including the plain line: *a yellow library warning is
   not permission to delete anything.*

## 4. Non-negotiables (merged, both brains)

1. **Never prune on a partial read.** Applies to playlists, the emergency mirror, index cleanup, and any
   future "remove missing" button. Unreadable ≠ deleted; DB list error ≠ empty.
2. **Never call a share outage "deleted".** Root probe fails ⇒ "Can't reach the music library" and the
   missing figures are shown *as of last full scan*, with the timestamp.
3. **No SMB work in UI or reports** — SQLite only + at most one `isdir(music_root)` per request. No
   `os.walk`, no per-row `isfile`, no tag reads in a request handler. Rescan lives on P3, throttled.
4. **Reports never block the feeder** — short read transactions, no writes, no network.
5. **Path-normalised joins everywhere** (§3.1), and **distinct paths, not skip events** (176–240 events
   ≠ 5 dead files).
6. **Move claims only on the existing relink ladder:** depth ≥ 2 suffix match, or unique same-size
   candidate, or unique basename with no size/duration conflict. Ambiguous → "several possible matches —
   needs you". Conflict → "same name, different recording — left alone". Never from a probe taken while
   the share is unreachable.
7. **Audio never stops.** No migration locks, no long write transactions, no "rebuild index" button in
   the UI.

## 5. Operator copy deck (John-facing, fixed wording)

| Reality | Say | Never say |
|---|---|---|
| `music_root` not a directory | "Can't reach the music library" | missing / deleted / anything counted |
| indexer proved the path gone | "Not found in the library" | "Deleted" (recycle may hold it) |
| playlist item with no present track (normalized) | "Won't play" | "Deleted" |
| item never indexed, under a reachable root | "Not in the index yet" | missing / dead |
| runtime skip | "Skipped on air" | library deleted |
| unreadable subtree during a scan | "Library check incomplete" | mass deletion |
| decode/codec failure | "Can't play this file" | missing from disk |
| unique-strong move candidate | "Probably moved to \…" | "is at", "deleted" |

## 6. Deferred (and why)

Full 264k-path browser (panic + useless) · auto-relink on page load or on air (side effects on a flaky
share) · "these 37 moved to X" as fact (lying risk) · growth/trend charts (needs scan history we do not
store) · codec/decode deep-probe report (SMB cost near playout) · tag-completeness product (Mark's
interest, not John's) · folder-item live member enumeration (resolve at air time; show folder gone/ok
only) · auto-removing dead items (prune hazard) · rewriting `tracks` paths wholesale (normalise at read
time) · a new SPA.

## 7. Where the brains differed, and how it was resolved

| Split | Rosie | Elon | Resolution |
|---|---|---|---|
| Where the index numbers live | A full index report (Tier A/B/C fields) | No fifth nav item; report belongs under Reports | **One** "Library index — snapshot" report + one Library-health card; no new nav entry |
| Move candidates | 37,559 same-name+size pairs as a headline feature | Never claim a move as fact | Clustered "probably moved" **only** through the existing relink ladder, labelled probable, per-file confirm |
| Sort of the all-playlists grid | Pin on-air, then unplayable count, then % | Missing count desc, then lowest playable % | Pin on-air/emergency first, then unplayable count desc, then % desc |
| Library-wide missing number | Show present/missing/total on the report cover | Keep 188k-class numbers out of the operator's face | Cover shows present/missing **with the as-of timestamp**; the card shows playlist-referenced missing as the headline and the index figure muted |

Combined score: (8.7 + 8.8) / 2 = **8.8**.

## 8. Immediate actions outside the build

- **Set `path_aliases`** (Mark's call, live box): `\\KDPI-Media\music` → `Z:` and
  `//KDPI-Media/music` → `Z:`. Recovers the 93 alias-fixable paths and stops that class of live skip.
- **One index rescan** in an off-air window — clears the 84 stale-index flags and most of the 264k debt.
- **Recovery** for the ~478: folder-grouped report → NAS recycle bin (H2632301) → relink with operator
  confirmation.
- **No mass playlist rewrite.** 92% of items are fine; a 13,837-row rewrite to fix a read-time problem
  is the change most likely to break the station.
