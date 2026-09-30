# StudioFire auto-start — LOCKED CONSENSUS (Rosie + Elon, 2026-09-29)

**Status: LOCKED.** Two independent brains (Rosie = research, Elon = adjudication), each working from
the same brief and neither seeing the other's work, arrived at the **same verdict and the same score
(8.7 / 8.7)**. Per the multi-brain rule, a converged pair locks without a cross-review round.

**Ticket:** H2627207 (StudioFire: auto-start on boot). **Brief:** `Projects/.hermes/claude-handoff/studiofire-autostart-consensus-2026-09-29.md`
**Verdicts:** `…-rosie-verdict.md`, `…-elon-verdict.md`

---

## THE DECISION

> **Autologon (Sysinternals, LSA secret) + a Task Scheduler task "At log on / run only when the user
> is logged on" that starts `start-all.bat` — plus an external, silence-aware watchdog.**
> **NSSM/Windows services are REJECTED for any host that must emit audio — including the
> "service calls start-all.bat" variant.**

One line why: audio only exists in an **interactive user session**; services live in **session 0**,
which has no audio endpoint, so a service-hosted engine starts healthy and plays nothing.

### Mark's idea, answered definitively
**It does not work.** A service whose Application is `start-all.bat` is still a service: cmd, python
and mpv all **inherit session 0**. `start-all.bat`'s three windows would be session-0 consoles — not
your desktop, not your audio graph. Same failure, different service name, with the added harm that
NSSM would dutifully restart the silent stack, so it *looks* managed while the station is dead air.
The only way a service can reach the interactive session is `CreateProcessAsUser`/`WTSQueryUserToken`
(or PsExec `-i`), which still **requires someone already logged on** — so it cannot replace autologon,
it can only bridge after one. Rejected as the standard path.

---

## THE EVIDENCE THAT SETTLED IT

**Live, on our own box (decisive):** `.200` was converted to services at 19:59:03 and by 20:00:28 the
engine logged `audio output initialization failed` on **3,529 tracks and climbing** — while
`GET :8080/health` returned `{"ok":true,…,"version":"1.3.2"}` and `services.msc` showed the services
**Running**. Process-level health and SCM state can both be green on a silent station. mpv itself
launched fine (pids present) — it was the audio *output* that could not initialise.

**Platform (Microsoft):** services run in session 0, isolated and non-interactive since Vista;
UI0Detect (interactive services detection) was **removed** in modern Windows, so "allow service to
interact with desktop" no longer buys a desktop. Task Scheduler's own documented split: **"run only
when user is logged on" = that user's interactive session**; **"whether user is logged on or not" =
session 0**. NAudio maintainers state plainly that WASAPI playback from a service is a **platform
limitation, not a library bug**. No Microsoft-supported way exists to give a normal WASAPI app an
audio endpoint in session 0.

**Industry (the strongest external evidence):** documented auto-start recipes for Windows playout are
autologon + start in the user session — never "NSSM the engine into session 0".
* **PlayIt Live** (official FAQ): Windows automatic login via netplwiz → shortcut in `shell:startup`
  → in-app "On Startup → Automation ON". https://www.playitsoftware.com/FAQs/q/how-do-i-set-up-playit-live-to-start-playing-audio-automatically
* **RadioDJ**: desktop app, **not** a service; unattended = auto-login + startup/Task Scheduler with
  "run only when user is logged on"; community adds Startup Delayer/BIOS AC-restore.
  https://radiotechstudio.com/blog/radiodj-complete-setup-guide/
* mAirList / StationPlaylist / Rivendell: session/desktop autostart, not headless-session-0 audio.
* Vendor PDFs for WideOrbit / RCS Zetta are not public — marked UNVERIFIED, and low-impact.

**Autologon mechanics:** registry `AutoAdminLogon` + `DefaultPassword` stores the password in
**cleartext** (Microsoft says only for physically secured machines). **Sysinternals Autologon** stores
it as an **LSA secret** instead. Use the tool.

---

## OPTIONS — MERGED TRADE-OFF TABLE

| # | Option | Session | Audio | Cold boot, no human | Crash recovery | Security cost | Call |
|---|---|---|---|---|---|---|---|
| A | NSSM per module (`install-services.bat`) | 0 | **No** (proven on `.200`) | process yes / **sound no** | NSSM restarts a silent loop | low cred, high on-air | **REJECT** |
| B | NSSM whose App = `start-all.bat` (Mark's idea) | 0 (inherited) | **No** | same trap | same | same | **REJECT** |
| C | Task "at startup" + "whether logged on or not" | ~0 | **No** | false hope | limited | creds maybe stored | **REJECT** |
| **D** | **Autologon + task "at log on / only when logged on" → start-all.bat** | **interactive** | **Yes** | **Yes** (with autologon + BIOS AC-on) | task restart + watchdog | **medium: autologon secret, unlocked kiosk** | **PICK** |
| E | Autologon + `shell:startup` shortcut only | interactive | Yes | Yes | weak (no restart-on-fail) | medium | acceptable fallback |
| F | Service + `CreateProcessAsUser` into the console session | interactive *if logged on* | only after logon | **no** without autologon | complex | high | not primary |
| G | PsExec `-i <session>` from a boot service | varies | fragile | no | no | high ops cost | **REJECT** as architecture |
| H | Explicit `audio_device_guid` alone (stay in session 0) | 0 | **expected no** | n/a | n/a | low | not a fix |
| J | Split: core/worker as services, engine in user session | mixed | engine yes | needs autologon for engine | per-component | medium | optional, not smallest change |

---

## MERGED NON-NEGOTIABLES

1. **Playout (engine + mpv) must run in an interactive user session** (session ≥ 1) — never session 0,
   never "whether user is logged on or not".
2. **Cold boot must restore audio with no human at the console** ⇒ the console session must exist ⇒
   **autologon**, with the physical-security trade-off accepted and mitigated.
3. **`.222` (FM on air at 88.5) is not converted to services.** Ever, while it is the air chain.
4. **HTTP 200 is not proof of on-air.** Monitor play state *and* the air chain; an **external** watcher
   is required. `.200` falsified the localhost-health assumption.
5. **`.200` must leave service mode** before it is trusted again — removed, not "disabled but present".
6. **Smallest change that works:** keep `start-all.bat`; add autologon + at-logon task; remove the bad
   services; do not rewrite the stack into a service fantasy.
7. **Set an explicit `audio_device_guid`** (the physical output feeding the Barix) — hygiene/robustness
   once in the interactive session. It does **not** change the session-0 conclusion.
8. **Autologon must not use plaintext `DefaultPassword`** where a better option exists — use the
   Sysinternals tool (LSA secret).
9. **BIOS: restore on AC power loss = On** (both brains raised it; a power bump must boot, not sit off).

## WHAT'S FLEXIBLE
Dedicated playout user vs the existing console user · windows minimized vs visible · task delay
30–90 s vs immediate · exact alert channel (Slack/SMS) · whether core/worker ever split to services if
they never touch audio · whether the watchdog grows a peak-meter check in-process or leans on Barix
silence detection first · doc rewrite style.

## WHAT WE STOP DOING (the reject list)
`install-services.bat` as a safe one-shot on any audio host (it kills the console stack, installs
session-0 services, and can still report success while air is dead) · `ON-AIR-SETUP.md`'s "services =
production, console = testing" guidance (**reverse it**) · "services are the production setup" as a
story · relying on core `/health` as on-air proof · PsExec/session injection as a boot strategy ·
hoping an explicit GUID rescues session 0 · a watchdog that lives only on the box it watches.

## WATCHDOG DESIGN (watch the watcher)
**On-box tier (cheap, fast):** engine in normal play not emergency, content advancing (title/position
changing), process set present in an **interactive** session, `*_console.log` growing, no spamming
audio-init errors.
**Off-box tier (non-negotiable for unattended FM):** probe from **another host** — engine status API,
plus silence/level detection on the air chain (Barix status / a stream tap) rather than trusting the
app's own report. Alert to humans who can act (Slack/SMS), never just a log line on the dying box.
**Rule from Elon:** *if the only thing that knows the station is silent lives on the silent PC, you
have no watchdog — you have a diary.*

## BUILD QUEUE (what the consensus implies)
1. `.bat` for the **chosen topology**: enable Autologon (LSA) for the playout account + create the
   "at log on / only when logged on" task → `start-all.bat` + a restart-on-failure watchdog task.
   Idempotent, `-check` preview, uninstall. *(.200 first — it is the expendable box.)*
2. **Guard `install-services.bat`** so it cannot be run on an audio host by accident (refuse, or a
   hard warning), and ship the `ON-AIR-SETUP.md` errata.
3. `audio_device_guid` set explicitly on both boxes (Barix-feed endpoint).
4. External silence-aware watchdog + alert path.
5. `.222` cutover only in a planned off-air window, ears on 88.5, after `.200` has run clean for days.

## SCORES
Rosie (research) **8.7** · Elon (adjudication) **8.7** → **merged 8.7** — high evidence quality on
session 0, the Task Scheduler session split, Autologon/LSA and the vendor paths; the live `.200`
failure is decisive negative proof for services. Residual uncertainty is limited to enterprise
(non-public) vendor docs and lab-only GUID-in-session-0 testing, neither of which changes the pick.
