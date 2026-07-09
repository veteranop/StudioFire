[[01-Active-Revenue]]

# StudioFire — Project Plan v0.3
**Author:** Veteranop, LLC | **Date:** 2026-07-03 | **Status:** Planning complete — ready to scaffold
**v0.2:** Revised after external technical review. Phases restructured engine-first; fallback/failover moved to Phase 0; poller split into its own service; missing radio-staple features added to roadmap.
**v0.3:** Round-2 logic review incorporated — see §10 Runtime Logic Hardening (queue versioning, P1 play journal, primed next-track, sub-second failure detection, emergency-mode persistence, spot-clock rules).

## 1. Executive Summary
StudioFire is a lightweight, self-hosted radio automation platform replacing ZaraRadio at a small radio studio. It provides:
1. **Playout** — plays music from a Synology NAS (~4TB library) through a defined local audio path (sound card → Barix Instreamer → WireGuard P2P VPN → transmitter site).
2. **Playlist management** — build, save, edit, and schedule playlists via a dead-simple web GUI, usable by non-technical staff, reachable only from the LAN with authentication.
3. **Studio Monitor** — a live status dashboard polling Ubiquiti (UniFi) network gear, Barix encoder/decoder devices, and the transmitter, showing green/yellow/red health at a glance.
4. **Self-update** — checks GitHub for the latest release and prompts the operator to update (deferred to a later phase).

## 2. Environment (as-built)
- Network: Ubiquiti/UniFi throughout, rebuilt by Veteranop.
- Studio → transmitter audio path: local audio out → Barix → WireGuard point-to-point VPN → Barix decode at TX site → transmitter.
- Media: Synology NAS, ~4TB of music, accessed via SMB (or NFS) share.
- Current software: ZaraRadio (Windows). Assumption: playout PC is Windows; StudioFire should run on that same machine (audio output must be physical, on the machine wired to the Barix).

## 3. Feasibility Assessment
**Overall: HIGH.** Every component is proven technology. Risk is concentrated in exactly one place: the audio playout engine.

| Component | Difficulty | Notes |
|---|---|---|
| Web GUI + auth (LAN only) | Easy | Standard web app, session auth, bind to LAN interface. |
| Playlist CRUD + NAS library browsing/indexing | Easy-Medium | Index 4TB of tags into SQLite; watch for new files. |
| Audio playout (24/7, gapless, crash-resistant) | **Hard if built from scratch — Medium if wrapped** | Do NOT write a custom audio engine. Wrap a battle-tested one (see §5). |
| Studio Monitor (UniFi/Barix/TX polling) | Medium | UniFi has a local API; Barix devices expose HTTP/SNMP; transmitter depends on model (SNMP/web/serial). |
| GitHub auto-update check | Easy | Compare local version vs. GitHub Releases API; prompt user. |
| Scheduling (dayparting, top-of-hour events) | Medium | Needed eventually — Zara users expect time events (IDs, jingles at :00). |

**Build-vs-buy sanity check:** AzuraCast and LibreTime exist and are free. They are internet-streaming oriented (Icecast-centric), heavier, and not designed around "play out a local sound card to a Barix" + custom network monitoring. A purpose-built lightweight tool is justified here, especially with the Studio Monitor requirement, which nothing off-the-shelf does for this exact stack. Verdict: **not stupid — build it.**

## 4. The One Big Risk (read this part, Grok)
Radio playout must run 24/7/365 with zero dead air. The mitigation strategy:
- **Separate the playout engine from the GUI.** The engine is a small always-running service. The web GUI is just a remote control. If the GUI crashes, music keeps playing.
- **Use a proven audio backend**, controlled over IPC:
  - **Option A (recommended): mpv** — rock solid, gapless, scriptable via JSON IPC, runs fine on Windows, trivial to control (load file, crossfade via two instances or af=acrossfade). Lightweight.
  - **Option B: Liquidsoap** — the industry-standard radio playout scripting engine (used by AzuraCast/LibreTime). Extremely powerful (crossfades, fallbacks, silence detection built-in) but Windows support is weaker and the learning curve is real.
  - **Option C: libVLC** — solid, good bindings, slightly clunkier gapless behavior.
- **Fallback logic:** if the queue empties or the NAS drops, auto-play from a local "emergency" folder on the playout PC (never dead air, never dependent on the NAS being up).
- **Local cache:** optionally pre-copy the next N tracks from NAS to local disk so an SMB hiccup can't cause a skip.
- **Watchdog:** engine runs as a Windows service (NSSM or native) with auto-restart; GUI shows engine health.

## 5. Proposed Architecture
**Design law:** the ONLY process that must stay alive is the Audio Engine. Every other process
can crash, hang, be updated, or be killed and the station stays on air. No process outside the
engine is ever in the audio path.

```
┌─────────────────────── Playout PC (Windows, wired to Barix) — 4 isolated services ────────────────────┐
│                                                                                                        │
│  P1 — AUDIO ENGINE SERVICE (tiny, standalone, NSSM auto-restart, top priority)                         │
│  ┌──────────────────────────────────────────────────────────────────────────┐                          │
│  │ thin Python supervisor + mpv (JSON IPC)                                  │                          │
│  │ - queue persisted to local file (survives restart, resumes mid-queue)    │                          │
│  │ - plays ONLY local files (pre-cache dir + emergency folder), never SMB   │──► Sound card ─► Barix ──┼─► VPN ─► TX
│  │ - any "can't start next track" condition → seamless emergency-folder loop│                          │
│  │ - self-watchdog: position-advancing check, mpv restart, device GUID check│                          │
│  │ - ZERO dependency on Core, DB, NAS, or network to keep playing           │                          │
│  └────────────▲─────────────────────────────────────────────────────────────┘                          │
│               │ localhost-only IPC (Core pushes queue + reads status)                                  │
│  P2 — CORE / WEB SERVICE (FastAPI)                                                                     │
│  ┌────────────┴─────────────────────────────┐                                                          │
│  │ web GUI + auth, playlist CRUD, scheduler │   SQLite (WAL): library, playlists,                      │
│  │ time events; feeds engine queue; fills   │   users, config, play history                            │
│  │ pre-cache dir from NAS ahead of airtime  │────── SMB ──────► Synology NAS (4TB music)               │
│  └────────────┬─────────────────────────────┘                                                          │
│               │ spawns / job queue                                                                     │
│  P3 — INDEXER WORKER (separate process, throttled, low OS priority)                                    │
│  ┌────────────┴─────────────────────────────┐   4TB tag scans can peg CPU or OOM —                     │
│  │ incremental NAS scan + watcher → SQLite  │   isolated so it can never touch                         │
│  └──────────────────────────────────────────┘   playback or the GUI                                    │
│                                                                                                        │
│  P4 — MONITOR POLLER SERVICE (separate process; writes status to DB/file, Core just renders it)        │
└──────────┬─────────────────────────────────────────────────────────────────────────────────────────────┘
           │
Crash matrix: P2/P3/P4 die → audio unaffected (GUI dark, music on). P1 dies → NSSM restarts it
in seconds, it reloads its persisted queue and resumes. NAS dies → P1 plays cache then emergency.

P4 Monitor Poller (all probes async with hard timeouts + retry backoff; dashboard shows
last-known-good state + "last checked X ago"; a dead probe degrades to STALE, never blocks)
               ──► UniFi local API on UCG-Lite (gateway/switches/APs: up, latency, WAN)
               ──► Barix devices via HTTP CGI (streaming state, levels, link)
               ──► WireGuard tunnel (handshake age / ping across tunnel)
               ──► Transmitter: BW Broadcast TX150 via raw TCP to serial-to-IP adapter
                     (telnet-style command protocol: query fwd/refl power, PA temp,
                      voltage/current, alarm states — parse text responses)
```

### Suggested Stack
- **Backend:** Python 3.12, four separate processes/services (P1 engine supervisor — stdlib-minimal, no FastAPI/DB imports; P2 FastAPI core with WebSockets; P3 indexer worker with `mutagen`; P4 poller). SQLite in WAL mode (P2/P3/P4 only — P1 never touches it), raw JSON-IPC to mpv, `apscheduler` for time events. Each service installed independently via NSSM so they restart and update independently.
- **Frontend:** Server-rendered pages + HTMX/Alpine.js, or a small Vue/Svelte SPA. Priority: **big buttons, big fonts, drag-and-drop playlist building, obvious status colors.** No dense pro-audio UI. Tablet-friendly.
- **Auth:** local user accounts (admin vs. operator roles), session cookies, LAN-bind only, HTTPS optional (self-signed) since LAN-only.
- **Packaging:** single installer or one-folder deploy; runs as Windows service; GUI reached at `http://studiofire.local:8080`.
- **Logging:** `CHANGELOG.md` (human-readable release notes) and `logs/error.log` (rotating structured error log) in the repo/app structure from day one. Also a **play log** (what aired when — stations often need this).

## 6. Feature Breakdown by Phase (v0.2 — engine first, GUI second)

### Phase 0 — Bulletproof Engine Core (NO GUI — prove the audio before anything else)
The engine wrapper is the product. Nothing else gets built until this survives torture testing.
- mpv wrapper service (Windows service via NSSM): JSON-IPC control, load/play/stop/skip/volume, gapless queue advance.
- **Aggressive health monitoring:** periodic IPC `get_property` pings, process CPU/memory checks, playback-position-advancing validation (catches silent hangs after a "play" that never produces audio). Hung/dead engine → kill + restart + resume queue automatically.
- **Emergency fallback folder (local disk):** IDs/sweepers/filler that loops forever. Exact failover rule: *any* condition where the next track can't start (empty queue, NAS unreachable, decode failure, DB error) → seamless immediate cut to emergency folder. No gaps, ever.
- **Local pre-cache:** next N tracks copied from NAS to local disk before airtime; playback always reads local files, never streams from SMB mid-song.
- Audio output device persisted by **GUID/endpoint ID**, never friendly name; re-enumeration detection + alert.
- Minimal HTTP control endpoint (localhost only) — enough to drive the engine for testing, becomes Core's interface later.
- Rotating structured error log + play log (as-aired with timestamps) + CHANGELOG.md + **versioned config schema** (migration-ready from day one).
- **Exit criteria (hard gate):** 72+ hours continuous playback on the bench with induced failures — NAS yanked mid-track, mpv killed, corrupt file queued, PC rebooted, Windows Update restart, audio device unplugged/replugged — with zero dead air beyond the failover cut.

### Phase 1 — Web GUI + Library
- Library indexer: incremental scan of NAS share (mutagen tags → SQLite WAL), file-watcher for changes, background rescans; never full-rescan-by-default (4TB will hammer NAS + PC).
- Playlist builder: search, click-to-add/drag-drop, reorder, save/load/duplicate. Simple edit-locking (single-station scale; optimistic locking only if it actually bites).
- **Playlist data model supports dynamic item types from day one** (critical — painful to retrofit): `file` (fixed track), `folder-newest` (resolves to newest file in a folder at play time — how syndicated shows slot in), `folder-rotation` (round-robin through a folder — how spot rotations work). GUI may only expose `file` at first; the schema and queue resolver understand all three.
- Playback controls: play/pause/skip/stop, volume, now-playing + progress, queue view, **"Play Next" cue** (insert any track/spot immediately after the current song).
- **Big PAUSE AUTOMATION / RESUME button** (for live-on-the-mixer segments) and **EMERGENCY: force engine to fallback folder** button.
- Auth: admin/operator roles, session cookies, LAN-bind.
- Baseline health tiles in GUI (Phase 0 data): engine status, NAS reachability, playout PC CPU/disk/RAM.
- One-click backup/restore of DB + playlists (versioned exports).

### Phase 2 — Studio Monitor + Basic Time Events
- **Separate poller service** (own process — a hung poll can never touch playback/control). Async probes, hard timeouts, retry backoff everywhere.
- Plugins: UniFi local API (UCG-Lite, 60s default, configurable), Barix HTTP CGI (tolerant parsers, treat format as versioned), WireGuard handshake/ping, TX150 raw-TCP connect-query-disconnect.
- Dashboard tiles: green/yellow/red + plain English + **last-checked age + last-known-good state**; stale probe shows STALE, not false-red.
- Alert banner on red; basic **silence/failure detection surfaced to operators**.
- **Basic top-of-hour events** (legal ID insert) — Zara users expect this immediately; full scheduling waits.

### Phase 3 — Radio Polish, Spots & Syndication
- Full scheduling: dayparted playlists, jingle rotation, event priorities.
- **Spot/ad scheduler (folder-based, kept deliberately simple):**
  - Point a rule at a folder or a single file: "play one item from here every X minutes" or "at :15/:30/:45".
  - Folder rules rotate round-robin (`folder-rotation` item type from Phase 1) so all spots get even play.
  - Insertion is **song-boundary aware**: "every 20 min" means *after the current song ends past the 20-min mark* — never cuts audio mid-track.
  - Priorities: top-of-hour legal ID > timed spots > music. Spot separation (don't stack 2 spots back-to-back unless told to).
  - Every spot play lands in the as-aired log with timestamp — this doubles as the ad affidavit record for sponsors.
- **Syndication fetcher (automated show downloads):**
  - Per-show config: source (SFTP/FTP/FTPS host, path, filename pattern), fetch schedule (e.g., "daily 04:00"), destination folder, retention ("keep last N episodes").
  - Runs as a job in the worker process (P3) — network stalls can't touch playback; downloads to temp + verify + atomic rename so a partial file can never be queued.
  - Playlists reference the show via `folder-newest` — playlist never changes, fetcher keeps the folder fresh, newest episode airs automatically.
  - GUI: per-show status tile (last fetch OK/failed, newest episode name/date) + failure alert so a missed download is caught *before* airtime.
- **Cart wall / hotkey buttons** (fire jingles/IDs/promos manually — staff will ask for this).
- Crossfade / gap trimming; cue points; metadata editing in GUI.
- As-aired log export (CSV) for regulatory/records and sponsor affidavits.
- Email/notification alerts from monitor.

### Phase 4 — Deployment & Updates
- GitHub Releases version check; "Update available" prompt; guided update that **safely restarts the Windows services** (tested, config migrations run automatically via schema versioning from Phase 0).
- Installer/one-folder deploy, first-run setup wizard (NAS path, audio device, emergency folder, admin password).

## 7. Open Questions (answer before coding)
1. ~~Playout PC OS confirmed Windows?~~ **ANSWERED: Yes — dedicated on-air PC**, does nothing else. Ideal: StudioFire core + engine run there as services; staff use the web GUI from any other LAN device.
2. ~~Transmitter make/model?~~ **ANSWERED: BW Broadcast TX150**, reached by telnet-style commands over a serial-to-IP adapter on a specific TCP port. Monitor plugin = raw TCP client that connects, sends query commands, parses text telemetry (forward/reflected power, PA temp, supply voltage/current, alarms), disconnects. Caveats: (a) most serial-to-IP adapters allow only ONE concurrent TCP connection — the poller must connect briefly and release the port so humans can still telnet in manually; (b) poll interval should be modest (e.g., 30–60s); (c) exact command strings/port to be captured from a live telnet session before coding the plugin.
3. ~~Barix models?~~ **ANSWERED: HTTP CGI interface.** Monitor plugin = HTTP GET to the Barix status CGI endpoints, parse the returned status (stream state, levels, connection). Simple `httpx` polling; capture exact CGI URLs/response format from the live devices before coding.
4. ~~UniFi controller type?~~ **ANSWERED: UniFi Cloud Gateway Lite (UCG-Lite).** The controller runs locally on the gateway. Poll via the local UniFi Network API — create a dedicated local read-only admin (or API key, supported on newer UniFi OS) for StudioFire. Gives device up/down, uptime, WAN status, client list, and per-device stats over HTTPS on the LAN. Note: UCG-Lite is modest hardware — keep poll interval reasonable (30–60s).
5. ~~Live shows?~~ **ANSWERED: Yes, but out of band** — live mic goes raw mixer → Barix, never through StudioFire. Software stays automation-only. Note for GUI: operators need an obvious **PAUSE/STOP automation** control (big button) for when they go live on the mixer, and a clean resume.
6. ~~Crossfade required at launch?~~ **ANSWERED: Butt-cut is fine for MVP.** Crossfade stays a Phase 3 nice-to-have.
7. **Multiple simultaneous users editing playlists?** (Simple locking probably fine.)
8. **NAS access:** **PARTIAL — library is mixed MP3 + MP4 (M4A/AAC).** Both handled by mpv and `mutagen` tags — no problem. Still TBD: a read-only SMB account on the Synology for StudioFire.

## 8. Risks & Mitigations
| Risk | Mitigation |
|---|---|
| Dead air from crash | Engine as watchdogged service, separate from GUI; auto-restart; emergency local folder |
| NAS/SMB dropout mid-song | Pre-cache next tracks locally; fallback folder |
| Non-technical users break config | Role-based auth; operators can't touch settings; "reset to safe defaults" button |
| 4TB index scan slow | Incremental scanning; background indexing; index only changed files |
| Scope creep (it's radio software — endless features) | Phased plan above; ship Phase 1 before touching Phase 2 |
| Windows audio device weirdness (device renames, USB re-enum) | Persist device by GUID/endpoint ID, health check with alert, selectable in settings |
| mpv silent hang (plays command, no audio) | Playback-position-advancing watchdog check, not just process-alive |
| Hung monitor probe blocks system | Poller is a separate process; async + hard timeouts on every probe |
| Playout PC resource exhaustion (scan OOM, disk full) | Self-monitoring of CPU/RAM/disk with GUI tile + alerts; throttled background indexing |
| Update breaks running service | Versioned config schema + migrations from day one; update flow tested with service restart |

### Review dispositions (v0.2)
Accepted from external review: engine-first phasing; fallback/pre-cache moved to Phase 0 with torture-test exit gate; poller split to separate process; silent-hang detection; device GUID persistence; cart wall/hotkeys, backup/restore, PC self-monitoring, as-aired export added to roadmap; basic top-of-hour IDs pulled into Phase 2; config migration from day one.
Rejected/deferred: RadioDJ/PlayIt Live (custom Studio Monitor + GUI simplicity justify the build — reviewer agreed); heavyweight multi-user concurrency (single small station; simple locking, revisit only if it bites); Liquidsoap (weak Windows support — already excluded); installer code-signing (revisit at Phase 4, cost/benefit for a single LAN deployment).

## 9. What This Is NOT (v1 scope fence)
- Not an internet streaming server (Barix handles transport — we just feed the sound card).
- Not a live mixing console / mic processor.
- Not multi-station / multi-tenant.
- Not cloud-hosted — LAN-only by design.

## 10. Runtime Logic Hardening (v0.3 — binding spec for Phase 0)
Rules below are requirements, not suggestions. They exist because each one closes a specific
silence/race hole identified in adversarial review.

### 10.1 P1 failover — closing the silence gaps
- **Guaranteed-good last resort:** a baked-in source (ffmpeg-generated tone, or a configured
  station-ID file) that exists even if every file on disk is gone. Failover chain:
  next cached track → emergency folder → **cached music (precache dir, scanned fresh)** →
  baked-in asset. Silence requires FOUR failures.
- **Emergency folder validated at P1 startup** (probe-decode each audio file; quarantine
  failures, alert). The folder is OPTIONAL: when it's empty (the default install), real
  rotation music from the local precache is the filler tier — listeners hear music, not a
  loop. Curated filler (IDs/sweepers) still takes priority if a station adds some.
- **Pre-prime next track:** next queue item is loaded/primed in mpv (playlist preload) before
  the current track ends — a decode failure surfaces while audio is still playing, not after.
- **Watchdog at 1s cadence, two checks:** (a) IPC responsive, (b) position advancing when state
  = playing. Short-track handling: expect track-change events, don't just diff position.
- **Audio device open failure → emergency immediately** (no retry-timeout first). Retry device
  in background; pin by endpoint GUID; on any audio error re-query device list; fall back to
  default endpoint WITH alert rather than stay silent.

### 10.2 Queue integrity — P1 ⇄ P2 protocol
- **Monotonic `queue_version`** on every queue. P2 sends mutations with expected version; P1
  rejects stale mutations (P2 re-syncs). Force-replace allowed only with higher version.
- **P1 persists atomically** (temp + rename) on every mutation AND every track advance:
  `{queue_version, entries, current_index, emergency_mode, timestamp}` — one file, one writer (P1 only).
- Track advance and mutation application are serialized inside P1 (single queue-owner thread);
  IPC is non-blocking with acks for critical ops; mutations are idempotent (carry IDs).
- **`emergency_mode` flag persists** — P1 restarting mid-emergency re-enters emergency
  immediately, then periodically scans pre-cache for playable items to resume normal queue.

### 10.3 Pre-cache contract
- Configurable target, default ~45 min of audio. Aggressive prefetch on queue change,
  background top-up, evict after airplay.
- Per-file: download to temp → size/verify → atomic rename. **Manifest file lists valid items**;
  P1 plays only manifest-listed files — partial copies from a P2 death mid-transfer are invisible.

### 10.4 As-aired integrity
- **P1 owns an append-only local JSONL play journal** (track start/end, timestamp, path, source
  type; human-readable; rotated). Written by P1 the instant events happen.
- P2 ingests on (re)connect: merge by time, dedupe, store in SQLite, drive GUI/exports.
  P2 downtime can never lose affidavit/regulatory data.

### 10.5 Scheduler clock rules (Phase 3, decided now)
- Interval rules ("every X min") use the **monotonic clock**; wall-clock only for fixed times
  (:00/:15...) with hysteresis. Immune to NTP jumps and DST.
- Per-rule due-tracking: fire **at most once per window**; missed windows don't stack repeats.
- Rotation index + due times persisted (survive restarts; no unfair skip/replay).
- Empty rotation folder = skip + alert, never a failover event. Scheduler faults must never
  break the P2→P1 feed.

### 10.6 Windows deployment specifics
- WASAPI **shared mode default** (dedicated PC; stability > latency here); exclusive mode as a
  tested config option only.
- NSSM: restart delay ~5s, cap attempts (e.g., 10/60s) then alarm; P1 writes heartbeat file.
  Service set to auto-start, survive logoff/fast-startup; power settings locked (no sleep).

### 10.7 Torture-test matrix (Phase 0 exit gate, expanded)
Kill P2 mid-pre-cache copy · kill P1 exactly at a track boundary · corrupt a manifest-listed
cached file · corrupt an emergency-folder file · queue a zero-byte/odd-codec M4A · flood P1
with queue mutations during track advance · system clock jump forward/back · unplug/replug
audio device mid-play · Windows Update reboot · NSSM rapid-restart loop · NAS offline for
hours (cache exhaustion → emergency → recovery) · P1 restart while in emergency mode.
Pass = zero silence >2s across all scenarios over 72h.
