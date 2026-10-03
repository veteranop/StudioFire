# StudioFire — the watchdog design (watch the watcher)

**Status:** design / partially implemented. Companion to
`docs/AUTOSTART-CONSENSUS.md` (which locked the auto-start architecture, 8.7/8.7)
and `docs/ON-AIR-SETUP.md` (the deployment runbook).

> **Rule (Elon, consensus 2026-09-29):**
> *If the only thing that knows the station is silent lives on the silent PC, you have
> no watchdog — you have a diary.*

The `.200` incident is the proof. Services were Running, core answered
`GET /health {"ok":true,"version":"1.3.2"}`, and the engine was burning through 3,529
tracks with `audio output initialization failed`. **Everything on the box said it was
fine.** A watchdog has to notice that from *outside*, and it has to reason about
**silence**, not about process state.

---

## The two tiers

### Tier 1 — on-box (cheap, fast, already built)

`scripts\watchdog.bat`, run by the **StudioFire-Watchdog** scheduled task every 5
minutes **in the interactive session** (`scripts\install-autostart.bat` creates it).

It runs `scripts\healthcheck.py`, which checks:

| Check | What it catches |
|-------|-----------------|
| `GET :8080/health` on core | the web/API layer is gone |
| `GET :7701/status` on the engine | engine unreachable |
| `mpv_alive` | the audio process died |
| `emergency_mode` and not `forced_emergency` | **exactly the `.200` symptom**: alive but looping filler because the queue is not being fed |

Behaviour: restart the stack **once**, and only after **two consecutive** failures, so a
single hiccup cannot drop audio for no reason. Logs to `logs\watchdog.log`.

**What Tier 1 cannot do:** it cannot hear the transmitter. It only proves the web layer
and the engine's own opinion of itself. It also cannot survive its own host going
silent — which is precisely when you need it.

### Tier 2 — off-box (non-negotiable for unattended FM)

The watcher must live on **another machine** and reason about the **air chain**, because:

- a hung or wedged box cannot be trusted to report that it is hung;
- the app's self-report is a hypothesis, not evidence;
- HTTP 200 is not on-air.

**Already built and running off-box:** the Hermes cron job *"KDPI chain watchdog (Phase 0
+ gated recovery)"*, every 5 minutes, via `scripts/kdpi_report/kdpi_watch.py` /
`kdpi_watch_cron.py`. It probes the **transmission chain from outside the StudioFire
PC**:

1. studio Barix Instreamer `192.168.6.51` — `GET /status` -> **input audio level** + uptime
2. mountain Barix Exstreamer `192.168.3.52` — decoder liveness
3. TX150 exciter via the Moxa gateway `192.168.3.200:4001` — one CR, read, close
   (the exciter is **alert-only**; there is no remote reboot, and one stray keystroke on
   that serial port can MUTE R.F.)

It is edge-triggered (silent when healthy), dwell-based on the silence decision (a song
fade or a quiet passage must not fire a false "off air" alert and get the watchdog muted
within a week — see the hardening note in `kdpi_watch.py`), and opens a TimeTrax ticket
on a new incident.

**This is the tier that actually protects the carrier.** The Barix input level is the
closest thing to "is audio arriving at the STL", and it is measured off-box.

---

## The gap to close

The two tiers currently watch **different halves** and do not talk:

| | Watches | Blind to |
|---|---|---|
| Tier 1 (`watchdog.bat`) | StudioFire **software** state, on the box | the air; and its own host dying |
| Tier 2 (`kdpi_watch.py`) | the **air chain** (Barix/TX150), off-box | the StudioFire PC's software state (engine in emergency with the Barix still passing whatever it is fed) |

The correlation that matters is: **the engine is in emergency/failed on the StudioFire
PC *and* the studio Barix input level is at the meter floor.** That combination is
unambiguous dead air, and it is the thing neither tier can see alone.

### Recommended extension (smallest useful change)

Add a **StudioFire probe** to the existing off-box `kdpi_watch.py` run, alongside the
Barix/TX150 checks:

- `GET http://192.168.6.200:8080/health` and `:7701/status` (the studio PC) — or the
  on-air box when `.222` is the one under test;
- from that status, the same fields Tier 1 uses: `mpv_alive`, `emergency_mode`,
  `forced_emergency`, `now_title` advancing;
- **correlate**, do not alert on each in isolation: engine emergency **AND** studio Barix
  level at the floor for the dwell window => one incident, one ticket, one alert. Engine
  emergency alone may just be a queue hiccup the on-box watchdog will restart; a Barix
  level dip alone may be a fade.
- **Verify the session, not just the process.** A service in session 0 and a console
  stack in session 1 look identical over HTTP. Where possible, additionally assert the
  stack is in an **interactive session** (e.g. `query session` / the task ran under
  `InteractiveToken`) so a silent-services regression is caught as its own incident.

### "Watch the watcher"

- The off-box cron must have its **own** heartbeat that a human can see; if the cron
  silently stops, the estate is unmonitored. (The Hermes job runner already records
  `executions.db` / `ticker_heartbeat` — surface a missed-run alert rather than assuming
  silence means health.)
- **Alert to humans who can act** (Slack / SMS via the existing VeteranOp channels),
  never just a log line on the dying box.
- Keep the recovery actions **gated**: Phase 0 is monitor/alert/ticket only. The Barix
  soft reboot already exists and is rate-limited + ticketed; the exciter stays
  alert-only.

---

## What `healthcheck.py` does NOT prove (say this out loud)

- That the **transmitter** is on. It checks the box, not the air.
- That audio is **audible**: an engine can report "playing" with the output device open
  on the wrong endpoint. That is why `scripts\set-audio-device.bat` pins the Barix-feed
  device by GUID — but the only true proof is a level on the air chain.
- Anything about its own host being down. A watchdog that cannot fail loudly is not a
  watchdog.

**Acceptance test for the whole design:** yank audio (stop the stack, or kill mpv) on the
on-air box while it is live. Within one Tier-1 interval you should see a restart attempt
in `logs\watchdog.log`; within one Tier-2 interval you should get an off-box ticket/alert
that names the studio Barix level, not just "a process is not running".
