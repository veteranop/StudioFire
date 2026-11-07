# StudioFire — the watchdog design (watch the watcher)

An unattended FM station needs more than "the process is running". This note is
about the layers that keep a station on air when nobody is looking, and what each
layer can and cannot prove.

> Station-specific values (which PC, which encoder, which transmitter box, the LAN
> addresses) are deliberately not in this file. They live in the private
> `site-notes/` folder for each installation.

## The two tiers

### Tier 1 — on-box (cheap, fast)

StudioFire installs as Windows services:

* `StudioFireCore` — the web UI and API (default `http://127.0.0.1:8080`).
* `StudioFireWorker` — indexing, tag reads, library scans.
* the audio engine + supervisor — the process that actually feeds the playout.

The supervisor restarts the engine if it exits, and the core reports a single
health verdict (`/api/health/verdict`) built from facts it can see: engine
reachable, audio not silent, a spot policy exists, the music library resolves,
nothing important is missing.

Autostart is two independent things, and they are easy to confuse:

| Layer | What it does | Where it is configured |
|---|---|---|
| **Service autostart** | starts StudioFire at boot, before anyone logs in | `scripts/install-services.bat` |
| **Autologon** | logs a Windows user in at boot so a desktop app can run | `scripts/autologon-lsa.ps1` (optional) |

A station that only needs the web UI + engine does **not** need autologon. A
station that also wants the GUI visible on the studio monitor does. The installer
sets services up silently; autologon is offered later, with a plain warning, and
is never done without a deliberate choice.

### Tier 2 — off-box (the part that matters for FM)

Everything on the box can be wrong at once: power, Windows, the service, the
network card. An off-box check is the only one that can notice **the box itself**
is gone.

The minimum useful shape:

1. Probe the **transmission chain** from outside, not just StudioFire —
   encoder/decoder liveness and, where the hardware allows it, the exciter's
   forward power or audio level.
2. Probe StudioFire's own `/api/health` and the engine's `/status`.
3. Raise a ticket or alert when a probe fails twice in a row, and again when it
   recovers — a flapping link is a fault, not a success.

Use documentation addresses in examples (`192.0.2.10`, `198.51.100.20`,
`203.0.113.30`); never a station's real LAN addresses in the repo.

## The gap to close

Tier 1 says "the software is fine". Tier 2 says "the station is on air". Neither
proves the other:

* Tier 1 can be green while the transmitter is fed silence (the software has no
  way to hear its own output).
* Tier 2 can be red while the software is perfectly healthy (the link, not the
  box).

That is why the health verdict is deliberately narrow, and why the off-box probe
is described as non-negotiable for an unattended FM station.

### Recommended extension

Add a StudioFire probe to the off-box run alongside the existing hardware probes:

* `GET http://<studio-pc>:8080/health` and `:7701/status`.
* Treat "unreachable twice" as a fault with its own message, distinct from
  "engine in emergency".
* Record the check time so a human can tell a stale report from a live one.

### "Watch the watcher"

The off-box probe is itself a single point of failure. The cheap answer is to run
it from somewhere else as well (a second host, or a hosted cron), and to make each
run say **when it last ran** so silence is visible.

## What a health check does NOT prove

Say this out loud when discussing monitoring:

* it does not prove the audio is intelligible, only that samples are flowing;
* it does not prove the transmitter is on the air, only that the chain answers;
* it does not replace a listener — a human hearing the station is still the
  ground truth.

Keep the private runbook (which box, which address, who to call) in
`site-notes/`, and keep this file about the design.
