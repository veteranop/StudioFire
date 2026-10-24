# Now-Playing → StreamGuys — CONSENSUS (locked 2026-10-09)

**Ticket #747 (M2628201)** · brains: Rosie 8.7 (research) · Elon 8.7 (adjudication) · Sr Dev 8.5
(implementation) → **merged 8.6**. Verdicts: `.hermes/claude-handoff/kdpi-nowplaying-push-2026-10-09/`.

## The problem (verified live 2026-10-09)
The SGplayer embed on **kdpifm.org** shows song info blank. The mount
`peridot.streamguys1.com:5115/live` **advertises `icy-metaint:16000` but publishes
`StreamTitle=''`** — the plumbing exists and **nobody is filling it**. The player config already has
`metaData.enabled` + a SGmetadata scraper (`jetio` + key + `scraperId`). **The display half is done;
the feed half is empty.**

## DECISION
1. **Ask StreamGuys first (gate G0).** The zero-code win, if it exists, is **SmartMetadata**
   (StreamGuys fingerprints the audio and serves titles server-side). Ask Corey whether it is
   **entitled/active for this mount** and why the player is blank. *If it is a config flip, take the
   win and leave StudioFire untouched.*
2. **If a push is required:** build a **P2-only, feature-flagged-OFF, fire-and-forget pusher** that
   reads StudioFire's now-playing and calls StreamGuys' **confirmed** ingest. Default transport =
   **Icecast `updinfo`** (`GET /admin/metadata?mount=/live&mode=updinfo&song=Artist%20-%20Song` +
   Basic) **or** the vendor API — whichever StreamGuys names. Transport is **pluggable**.
3. **Origin: NOT `.222`, NOT the Barix, NOT `supervisor.py` (P1).** Hook = the **P2 journal-ingest
   path** (`services/core/engine_bridge.py` feeder loop, after `ingest_journal`). P2 already does
   network (GA4, update-check); P1 stays **stdlib-only, off-net, out of the metadata path**.
4. **`.6.100` stream box** — the "purest" home for *ICY-source* metadata, but its software is unknown
   until site visit **#655**. Keep StudioFire as the single source of truth and push *from* P2;
   revisit `.6.100` as a *transport choice* only after #655.
5. **SoundExchange stays separate.** This is **cosmetic player display**. Reports of Use are batch
   records from the local air log + CDN ATH — do **not** conflate. The #720 local durable log stands.

## Options (merged trade-off table)
| Option | Push? | Vendor $ | Plant risk | Verdict |
|---|---|---|---|---|
| **A. SmartMetadata enable/repair** | No | entitlement? | None | **Try first (if entitled)** |
| **B. Encoder-native ICY on `.6.100`** | Yes | 0 | Web-stream | Only after #655 names the stack |
| **C. Off-box P2 sidecar (updinfo/API)** | Fire-and-forget | 0 | None (off air path) | **Default if A unavailable** |
| **D. P1/`.222` push on track_start** | Yes | 0 | **FM path** | **FORBIDDEN default** |
| **E. Barix inject** | Yes | 0 | **FM path** | **Reject** |
| **F. Public site → StudioFire LAN API** | — | — | Unsafe | **Reject** |
| **G. Do nothing** | — | — | Blank player | **Reject** |
| **H. Fake "SGmetadata write"** | — | — | Waste | **Reject** (SGmetadata is **read-only**) |

## NON-NEGOTIABLE
- Push originates in **P2**; **never** in `supervisor.py`/P1. P1 stays stdlib-only, off-net, out of the
  metadata path.
- **Enqueue-only** on the feeder thread; all HTTP on a **dedicated worker** with a hard
  connect+read timeout; **never raises**; silent no-op on failure. (Feeder-thread stall ⇒ starved
  precache ⇒ failover risk — this is *the* reason for the worker.)
- **Feature-flagged, OFF by default.** Logging never depends on the push; disabling it changes nothing.
- **Gate the real transport on StreamGuys' written answer** (method + endpoint + auth + SmartMetadata
  entitlement) — the 2026-10-05 consensus holds.
- **Local-first logging** — CDN down ⇒ air log still works.
- **Do not conflate live now-playing with SoundExchange RoU.** Station stays licensee.
- **Secrets in `Projects/env/` (gitignored)**, never in git, config examples, the player config, or the
  public scraper key's neighbourhood.
- **No live-plant probe/command** in the design phase; no Barix/STL change for a website widget.

## FLEXIBLE
- SmartMetadata-only vs sidecar vs encoder-native (Smart as backfill).
- Transport: updinfo vs vendor API (pluggable); song string single `"Artist - Song"` vs split
  artist/title/art (resolve the fed entry like `/api/queue`); artwork best-effort.
- Push-origin box: **`.200` preferred** (core/P2 home); confirm at build; `.6.100` later.
- Throttle window, spot/ID inclusion — config knobs.

## BUILD QUEUE
0. **GATE** — no real transport until StreamGuys answers. Build **scaffold + `DryRunTransport`** now.
1. **Config + flag** (`config.py`): `nowplaying_push` block — `enabled:false`, `transport`, `endpoint`,
   `mount`, `auth` (env), `timeout_sec`, `min_interval_sec`, `include_spots:false`. Tests assert
   default-off + env-sourced secret.
2. **`services/core/nowplaying_push.py`:** `NowPlayingPusher` (bounded queue, worker thread, hard
   timeout, drop-on-full, never raises) + `Transport` interface + `DryRunTransport`. Dedup consecutive
   titles; `min_interval` throttle; skip `title is None`; persist last-pushed `journal_id` (no history
   replay). Unit tests, **zero network**.
3. **Wire into ingest:** surface just-ingested `track_start` events from `ingest_journal`; feeder loop
   calls `pusher.on_track_start(ev)` after ingest; on backlog push **only the current** track
   (reconcile vs `/status` `now_id`). Tests.
4. **Real transports** (post-G0): `IcecastUpdinfoTransport`, `HttpJsonTransport`; integration-test vs a
   **local mock Icecast only — never the live KDPI mount**.
5. **Ops surface:** last-push status + failure log; admin "send test push"; DEPLOY.md note; confirm
   push-origin box.
6. **Live enablement:** controlled push to a non-air mount → flip `enabled:true` with Liz/Corey watching
   the SGplayer. Reversible by one flag.

## Exact questions for StreamGuys (G0) — sent 2026-10-09
Why is `StreamTitle` empty while `icy-metaint` is set? Is SmartMetadata entitled/active for KDPI, and
does SGplayer use scraper, Smart, or merge? Supported **write** method for this mount (exact URL, auth
class, same-IP-as-encoder?, ICY vs HLS)? May an off-encoder studio-LAN host call it? Cost if Smart is
off? Recipe they give other automation customers?

## What did NOT change
No plant code. No live device touched. No StreamGuys feature built. The #720 SoundExchange track is
untouched. StudioFire's air log remains the system of record. The blank title persists until G0 closes
(or a confirmed push ships).

## Billing note
StudioFire code = **free to John**. Website / CDN / StreamGuys coordination = **KDPI service work**
(billable). Do not mis-classify.

## Related
`docs/2.0-Design-Decisions.md` (local-first law, P2-push line) · `docs/AUTOSTART-CONSENSUS.md` (same
pattern) · KDPI tracker #297 · research #720 · vendor ticket MOT-259-86164.
