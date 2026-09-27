---
tags: [reference, studiofire, training, documentation, ux]
status: active
created: 2026-09-14
up: "[[StudioFire/docs/Home]]"
---

# StudioFire Training Guide — Best Practices Research

Research + recommendations for building an **extremely approachable** training guide for
StudioFire (broadcast/radio automation), for radio-station staff who are NOT IT people —
and how the same content becomes an online module on `training.veteranop.com`.

Research date: 2026-09-14. Sources listed at the end.

---

## 0. TL;DR — the eight rules that matter most

1. **Organize by the operator's job, not by the software's features.** "Put a show on air
   tonight" beats "Scheduler module."
2. **One task per section; one screen per task.** The reader should never scroll past a
   screenshot to find the button it refers to.
3. **Screenshot-first, text-second.** Every step gets a picture; the picture carries the
   instruction, the text just confirms it.
4. **Annotate every screenshot** — numbered markers matched 1:1 to numbered steps. An
   unannotated screenshot is a decoration, not an instruction.
5. **Pre-empt panic.** Radio staff fear dead air and fear "breaking it." Reassure
   up-front: what is safe to press, what the system does on its own, and what to do if
   it looks wrong.
6. **Plain English, short sentences, active voice.** Ban the jargon; keep a glossary for
   the words you can't avoid (and prefer the words the UI actually shows).
7. **Show the expected result after every step.** "You should now see …" closes the
   anxiety loop — this is the single most-skipped and most-valuable element.
8. **Avoid a monolithic PDF.** A 60-page manual nobody reads loses to a 1-page quick
   card + ten 2-page task sheets + a searchable online module.

---

## 1. Know the audience: adult, anxious, task-driven

Two established frameworks explain why typical software manuals fail here.

**Andragogy (Knowles) — adult learners:**
- Need to know *why* before they invest ("why does this matter to my shift?").
- Are **problem-centered, not content-oriented** — they want the task solved, not the
  system explained.
- Bring experience (many DJs came from ZaraRadio or from paper logs) — build on it,
  don't erase it.
- Are self-directed and will skip anything that looks like a schoolbook.
- Want immediate relevance — every section must map to something they do on a shift.

**Technology Acceptance Model (Davis) — why people actually adopt software:**
Adoption hinges on **perceived usefulness** and **perceived ease of use**. A training
guide's job is to raise both: show that StudioFire does the job better (usefulness) and
that the reader can operate it confidently (ease). Confidence, not completeness, is the
deliverable.

**Practical implication:** the guide's success metric is *"a nervous volunteer DJ can put
the station on air alone at 6am"* — not *"we documented every setting."*

---

## 2. Separate the learning from the lookup (Diátaxis)

The Diátaxis framework splits documentation into four kinds. Conflating them is "at the
root of many difficulties that afflict documentation." For StudioFire, use **two**:

| Kind | Purpose | StudioFire artifact |
|------|---------|---------------------|
| **Tutorial** (learning-oriented) | A guided first experience that builds confidence. Concerned with the learner *acquiring skill*. | "Your First Hour: from login to on-air" — a scripted walkthrough the trainer leads. |
| **How-to guide** (task-oriented) | Directions to accomplish a specific real-world goal by an already-competent user. Only action; no teaching, no digression. | "How to add a spot," "How to schedule a weekly show," "How to export the as-aired log." |
| *(Reference)* | Dry facts, for lookup. | Settings reference, glossary, port list — online only, not in the printed guide. |

Key Diátaxis rules that map directly onto this project:
- **Write how-to guides from the user's perspective, not the machinery's.** The framework
  calls out `"To deploy the desired database configuration, select the appropriate
  options and press Deploy"` as a *non-example*: it is a cause-and-effect description of
  a machine, addressed to no human need. Rewrite as: *"To have the new ad play every hour
  at :20, do this…"*
- **Assume competence in the domain** (they know radio) and give **no digression** —
  link out to explanation instead of inlining it.
- **Tutorials must deliver visible results early and often**, and **maintain a narrative of
  the expected**: *"You will notice …"; "After a few seconds, you should see …"; "If the
  output doesn't show …, you probably forgot to …"*

---

## 3. Structure recommendation — the guide as a set, not a book

### 3.1 The three-tier kit

Do not ship one big PDF. Ship a small kit; each piece has one job.

```
TIER 1 — QUICK-START CARD           1–2 pages, laminated, taped by the console
   "On Air in 5 steps" + "If something goes wrong" + emergency numbers

TIER 2 — TASK SHEETS                2 pages each, one task per sheet
   ~8–12 sheets: Go on air · Stop/pause · Build a playlist · Start a show
   · Add a spot · Export an as-aired report · Add a library folder
   · Add equipment to the monitor · Add a user

TIER 3 — THE FULL GUIDE             tutorial (scripted) + how-tos + glossary
   For training day and for the shelf; also the source for the LMS
```

Why: the operator under pressure (it's 6:00 and the automation won't start) does not
want a 60-page book. They want a card. The full guide is for learning and for the rare
deep task.

### 3.2 Front matter that must exist (the guide earns its first 3 pages)

- **Cover** — product name, version, date, "for KDPI / Drop-in Radio" style label.
- **"What StudioFire does in one paragraph"** — plain English, includes the promise
  (*audio never stops*).
- **"The five things you need to know"** — the mental model. For StudioFire:
  1. The music keeps playing even if the web page closes — the engine is separate.
  2. Green = on air and healthy, red = a problem, yellow = a warning. (Match the GUI.)
  3. Anything you do in Playlists only affects what plays *later*, not right now.
  4. If the page freezes, **do nothing for 30 seconds** — audio is unaffected.
  5. When in doubt, don't fix it; call the number on the card.
- **"Where everything is"** — one annotated screenshot of the nav
  (On Air · Playlists · Schedule · Settings · Reports) with each labelled in plain terms.
- **Table of contents with task names**, not feature names.
- **"Who to call"** — always near the front, not just the back.

### 3.3 Per-task section template (repeat verbatim for every task)

Consistency is what makes a guide feel approachable. Use the same skeleton every time:

```
① What this does, and when you'd want to        (2 sentences, in the operator's words)
② Before you start                              (1–3 prerequisites, if any)
③ The steps                                     (numbered; each step = 1 screenshot)
      Step N  [verb-first instruction, ≤ 12 words]
              [annotated screenshot]
              → "You should now see …"
④ What just happened / what's safe              (optional, 1 line)
⑤ If it doesn't work                            (the 2–3 most common problems)
⑥ Related tasks                                 (links to other sheets)
```

Rules baked into the template:
- **One idea per sentence.** Google's tech-writing rule: split multi-thought sentences;
  convert embedded lists to bullets.
- **Verb-first step titles.** "Click Settings," not "The Settings button."
- **Cap it at 2 pages.** If a task needs 4, it's two tasks.
- **End with a "Done" affirmation** — "That's it — the spot will now play every hour at
  :20." Adult learners need the success signal.

### 3.4 Back matter

- **Troubleshooting** organized by *symptom the operator sees*, not by cause:
  "The screen says AUDIO ENGINE NOT RESPONDING," "Music is playing but the title is
  blank," "My show didn't start," "A song is underlined / won't play."
- **Glossary** — every unavoidable term defined once, in one sentence, alphabetically
  (pre-cache, rotation, spot, .lst, failover, feeder, journal, as-aired, UNC path).
- **Cheat-sheet page** — one page, the whole product reduced to boxes and arrows.
- **Version + "regenerate from source" note.**

---

## 4. Screenshot & visual design best practices

### 4.1 Screenshot hygiene (the make-or-break details)

| Rule | Why / how |
|------|-----------|
| **Consistent viewport** | Same browser, same window size, same zoom for *every* shot. Standardise on one size (e.g. 1440×900 at 100%). Nothing looks less professional than seven different crop shapes. |
| **Real, realistic data** | Use plausible real content ("Now playing: Emmylou Harris"), not `test1.mp3` or `foo`. Fake-looking data reads as untrustworthy and doesn't teach recognition. |
| **Crop hard** | Show only the region the step concerns, plus a little context. Full-screen shots at 30% zoom are unreadable on paper. |
| **Highlight, don't describe** | A red/orange rectangle + a numbered circle on the exact control. The eye finds the box before the text. |
| **Number the markers to match the steps 1:1** | Step 3's instruction refers to marker ③ in the picture. This is the single highest-value annotation pattern. |
| **Show the result state too** | A "before" and "after" pair when the result is visual ("ON AIR" turning green). |
| **One screen per step** | Never make the reader cross a page break to find the button a step names. |
| **Pixel discipline** | Export at 2× and downscale, or use vector/HTML-rendered UI. Blurry screenshots undermine "ease of use." |
| **Re-capture on UI change** | Build the guide *from* the app so screenshots can be regenerated. See §8.5. |

### 4.2 Apply Mayer's multimedia principles

These are the evidence-backed rules for pairing words and pictures (Mayer):

- **Multimedia:** words + pictures beat words alone → screenshot every step.
- **Spatial contiguity:** put the text *next to* the part of the picture it describes —
  not in a legend at the bottom of the page.
- **Signaling:** highlight the essential element (arrows, boxes, circles). This is exactly
  the numbered-marker pattern.
- **Coherence:** strip extraneous material — no decorative clip-art, no background music,
  no "fun" flourishes. On-screen clutter is a cognitive tax.
- **Segmenting:** break content into small, learner-paced pieces (→ one task per sheet).
- **Pre-training:** teach the vocabulary/parts *before* the procedure ("here is the
  cockpit, these are its five zones" before "how to go on air").
- **Redundancy (for video/audio versions):** do not put a full verbatim script on screen
  while narrating it; add *different* on-screen text, or none.
- **Modality (for video):** graphics + spoken narration beats graphics + on-screen
  paragraph. (This matters for the LMS video versions — see §9.)

### 4.3 Visual design specifics

- **Big type.** Body ≥ 11–12 pt in print; headings clearly larger. This audience skews
  older; small grey text is the #1 complaint about software manuals.
- **Strong contrast.** Dark text on white/very light background; never grey-on-grey.
- **Generous whitespace.** White space *before* a heading and around screenshots. Crowding
  is the visual signature of "hard to use."
- **Status colour, matching the app.** StudioFire's GUI uses green = on air / healthy,
  red = fault, yellow = warning. Use the *same* colours in the guide so the reader builds
  one mental model. **Never rely on colour alone** — always pair with a word/icon
  ("ON AIR" text, a red ⏹), for colour-blind readers and for photocopies.
- **Two or three fonts max.** One sans-serif throughout is safest (e.g. Inter, Segoe UI,
  Source Sans). Mono only for file paths.
- **Callout boxes, used sparingly:** ✅ Tip · ⚠️ Warning ("this stops the music") ·
  ℹ️ Note · 🆘 Emergency. A bot warning that is used everywhere is used nowhere.
- **Numbered steps in a distinct visual block**, indented from the screenshot so the eye
  reads instruction → picture → result.

### 4.4 Accessibility (also a procurement/ADA consideration)

- Contrast ≥ 4.5:1 for body text (WCAG 2.2 AA); ≥ 3:1 for large text.
- Don't rely on colour alone to convey meaning (status, errors).
- Support user text-spacing preferences online (WCAG 1.4.12: line height ≥1.5×, paragraph
  spacing ≥2×, letter spacing ≥0.12×, word spacing ≥0.16× must not break layout).
- For PDFs: use **tagged PDF / PDF-UA** where possible (ISO 14289) — logical reading
  order, real headings/lists/tables tags, **alt text on every meaningful image (every
  screenshot!)**, embedded fonts, Unicode-mapped text. This also makes the PDF reflow on
  phones and keeps it text-searchable.
- **Keep text as text.** Never bake instructions into an image — screen readers can't read
  it, search can't find it, and you can't fix a typo. (Images *of text* also break the
  WCAG text-spacing criterion.)
- Caption any video; provide transcripts.

---

## 5. Writing style for non-technical users

Distilled from Google's developer documentation style + Write the Docs:

1. **Second person, active voice.** "Click **Save**." Not "the user should save" or
   "the file will be saved."
2. **Short sentences, one idea each.** Split any sentence with "and/or/which/because of."
3. **Convert embedded lists to bullets.** ("When you see *or* in a long sentence, refactor
   to a bulleted list.")
4. **Verb-first, imperative steps.** "Choose the folder," "Type the name," "Press GO."
5. **Name UI elements exactly as they appear**, and bold them: **On Air**, **Stop after
   current Song**, **Studio health**. If the label is jargon, that's an app bug worth
   fixing — but the guide must match reality regardless.
6. **Kill the filler.** "at this point in time"→"now"; "is able to"→"can";
   "provides a description of"→"describes."
7. **Define or delete jargon.** Maintain a **jargon blacklist** and run the guide text
   through it before shipping (the reporting pipeline already does this for outreach
   letters — reuse the pattern). Candidate banned words for this audience: *service,
   daemon, process, restart the stack, queue protocol, IPC, SQLite, WAL, cache,
   pre-cache, failover, endpoint, deploy, config, UNC path, SMB, NAS* (say "the music
   drive"), *scheduler* (say "shows & times"), *feeder* (invisible — never mention).
8. **"What you should see" after every step.** The narrative of the expected.
9. **Flag the surprising.** "The page will reload — that's normal." "This takes about
   ten seconds."
10. **No hype, no apology.** "The system handles this automatically" beats "don't worry,
    it probably won't crash."

---

## 6. Common pitfalls (each of these has sunk a real guide)

**Structure & scope**
1. **Feature-outline instead of task-outline.** Chapters mirroring the app's menus, so the
   reader must know the app to find anything.
2. **One monolithic PDF.** Nobody reads it; nobody maintains it; it's stale in a month.
3. **Mixing tutorial, how-to, and reference** in one flowing document (the Diátaxis trap).
4. **FAQ-as-documentation** — FAQs go stale, accumulate unrelated content, and tempt you to
   avoid writing real docs.
5. **A "for IT" chapter bolted onto an operator guide.** If it needs a terminal, it belongs
   in DEPLOY/Operations docs, not the operator guide.

**Screenshots & visuals**
6. **Unannotated screenshots.** The reader plays "spot the button." Always box + number.
7. **Tiny, full-screen, zoomed-out shots** that are illegible in print.
8. **Inconsistent capture** (different window sizes / themes / test data).
9. **Stale screenshots after a UI change** — erodes trust faster than any typo. Fix by
   generating captures reproducibly (§8.5) and dating each shot.
10. **Text inside images** — unsearchable, inaccessible, un-fixable.
11. **Colour-only meaning** — "press the red one" fails for colour-blind readers and
    photocopies.

**Content & tone**
12. **Assuming prior knowledge** ("as you know, point the playlist at the share…").
13. **Explaining the machinery, not the task.** The Diátaxis "turn the tap clockwise"
    failure.
14. **No expected-result confirmation** after steps.
15. **No failure path.** A guide with no "if it doesn't work" section leaves the reader
    stranded at exactly the moment they need help.
16. **Jargon leakage** — every unexplained term is one user who stops and calls support.
17. **Burying "who to call"** in the appendix.
18. **No glossary**, so the same unexplained words recur.
19. **Talking down to the reader** or padding with obvious statements ("a mouse is a
    pointing device").
20. **Long paragraphs.** Nobody reads them; this audience scans.

**Process**
21. **Not testing the guide on a real operator.** Have one non-author staffer follow it
    cold and note every place they hesitate. That hesitation list *is* the bug list.
22. **No version/date on the guide** — nobody knows if it's current.
23. **No feedback loop** — add a "was this helpful / something wrong?" line so the guide
    improves like software.

---

## 7. Broadcast/radio-automation specifics

Radio automation guides have a unique emotional context: the operator is responsible for a
**live** stream, often alone, possibly at 5:45am, and **dead air is a crisis**. Design for
that.

1. **Lead with the promise and the safety net.** Open with "StudioFire is built so the
   music doesn't stop — even if this web page closes, the show keeps playing." Instant
   reduction of the #1 fear.
2. **Put emergency procedures early and make them unmistakable**, not in an appendix. The
   quick card's "If something goes wrong" panel is the most-read thing you will write.
3. **Distinguish "live" actions from "planning" actions.** Make it explicit that editing a
   playlist changes *later* playback, and that **GO / STOP** are the only two buttons that
   change *right now*. This prevents the most dangerous misconception (thinking an edit is
   live, or thinking a stop is temporary).
4. **Never instruct the operator to do anything that can cause silence without a warning
   box.** "⚠️ This stops the music" on exactly those steps.
5. **Teach the status language of the app**: ON AIR light, **Studio health** pill,
   the yellow "EMERGENCY FILLER IS ON AIR" banner, the red "AUDIO ENGINE NOT RESPONDING"
   banner. Explain what each means and what (little) the operator should do.
6. **Give the "do nothing" instruction a home.** "If the page freezes: wait 30 seconds.
   Audio is not affected. Do not restart the PC." — explicitly authorised inaction is
   powerful for nervous operators.
7. **Cover the shift-shaped tasks, not the module list.** The real StudioFire task set:
   - Go on air / take the station off standby
   - Stop, skip a song, or "stop after this song"
   - Build or edit a playlist (and where the `.lst` file lives) — including the
     ZaraRadio-compatibility reassurance
   - Schedule a show (once / daily / weekly; how it hands back to rotation)
   - Add a spot (folder / interval / minutes-past-the-hour / manual)
   - Check what actually played — run and export the as-aired report
   - Add music to the library (and why the indexer takes time; don't kill it)
   - Add studio equipment to the monitor and read green/red
   - Add or remove a user
   - What the restart ⟳ button does, and why it briefly "blips to filler"
8. **Frame the four-service architecture in one friendly image**, not in words. A single
   annotated diagram ("the engine is the only part that touches the audio — everything
   else can come and go") buys enormous confidence. Don't name the services P1–P4 to
   operators.
9. **Time-to-competence target.** State it and design to it: "Confidently on air in 30
   minutes." Build the tutorial to that budget.

---

## 8. Turning the guide into an LMS module on training.veteranop.com

### 8.1 Content model — microlearning

- **One module = one task**, 5–10 minutes. (The PDF task sheet maps 1:1 to a module — build
  once, publish twice.)
- **One course = a role**: "StudioFire for DJ / Operators" (core 6 modules),
  "StudioFire for Admins" (playlists/schedule/spots/reports/users), maybe
  "StudioFire for the Station Engineer" (services, restart, equipment monitor).
- **Bite-size is not dumbing-down** — it is the segmenting principle from §4.2 enforced by
  the platform. Learners can stop and resume (bookmarking).

### 8.2 Module skeleton (map to Gagné's nine events of instruction)

| Module step | Gagné event | StudioFire content |
|-------------|-------------|--------------------|
| 1. Hook / why | Gaining attention | "This is the screen you'll use every shift." |
| 2. Objective in one line | Informing the learner of the objective | "By the end you'll be able to start a scheduled show." |
| 3. Recall / connect | Stimulating recall of prior learning | "You've built a playlist — now schedule it." |
| 4. Walk-through (demo) | Presenting the stimulus | Screen-capture video or animated GIF of the exact clicks (≤ 60–90 s). |
| 5. Guidance | Providing learning guidance | Annotated screenshot + short text (reuse the PDF graphics). |
| 6. Practice | Eliciting performance | Interactive: hotspot click ("click GO"), drag-to-order, fill-in. |
| 7. Feedback | Providing feedback | Immediate, specific, kind ("Not quite — GO is the green button on the right"). |
| 8. Check | Assessing performance | 2–3 scenario questions, not trivia ("A song is playing and you must take a break in 4 minutes. What do you press?"). |
| 9. Job aid + transfer | Enhancing retention and transfer | Downloadable quick card (the PDF!) + "now do it on the real system." |

### 8.3 Interactive elements worth building

- **Click-the-hotspot images** (reuse annotated screenshots; the marker becomes the quiz
  target).
- **Short screen-capture videos / GIFs** for anything with motion (the ON AIR light
  changing, a drag-reorder). 60–90 s max, captioned.
- **Knowledge checks** with retry, and feedback that teaches rather than scores.
- **A safe sandbox / practice mode.** The highest-value LMS feature for this audience is a
  risk-free simulator. If StudioFire can run against a dummy library or a "practice"
  dataset, an LMS activity can let learners press GO/STOP with zero consequences. If not
  buildable, a scripted branching scenario ("what do you do next?") gets most of the way.
- **Scenario-based assessment**, aligned to the fear profile: dead-air recovery, "page
  frozen what now," "spot didn't play."

### 8.4 Platform & standards choices

- **SCORM** (1.2 / 2004) remains the most widely supported packaging standard — use it if
  you need the module to drop into a third-party LMS or track completion/score. Caveats:
  coarse tracking (mostly complete/incomplete), large packages (>500 MB) can fail uploads,
  and `suspend_data` limits bite on big bundles.
- **xAPI (Tin Can) / cmi5** is the modern alternative: finer-grained statements
  ("operator pressed GO in practice module"), works *outside* an LMS, and suits
  simulator/sandbox tracking. ADL stewards both.
- **For a small station / Mark's own `training.veteranop.com`:** a lightweight self-hosted
  LMS is plenty — e.g. **Moodle** (SCORM + xAPI natively, self-hostable, huge ecosystem) or
  **Open edX** if you want a MOOC feel. Authoring tools that export SCORM and are cheap for
  a small shop: **Articulate Rise/Storyline** (paid, easiest), **iSpring**, **Adobe
  Captivate**, **eXeLearning** (free), **H5P** (free, excellent for interactive
  hotspots/quizzes and embeddable in Moodle/WordPress). **H5P is the standout low-cost
  option** for click-hotspot and interactive video using the very same screenshots.
- **Booking/multi-tenant note:** keep internal-station content and public content separate
  (same discipline as the internal-vs-shareable report carve-out). Station-specific IPs and
  paths don't belong in a public module.

### 8.5 Single source of truth (make the PDF and the LMS share content)

- Author content once in a structured form (markdown or a small JSON/dict per task:
  `{title, why, steps[{text, image, marker}], result, trouble[]}`). Then:
  - a builder renders the branded **PDF** (reuse the existing `reporting-pipeline`
    HTML→headless-Chrome pipeline and brand constants), and
  - the same data drives the **LMS** (H5P/SCORM or HTML pages).
- **Capture screenshots reproducibly** from the running GUI with a scripted browser
  (Playwright) at a fixed viewport, so a UI change means "re-run the capture," not "re-shoot
  forty images by hand." Version-stamp every build with the app version.
- This kills pitfalls #9 (stale screenshots) and #22 (no version) structurally.

### 8.6 Measurement & iteration

Track, at minimum: module completion, quiz first-attempt pass rate, and **time-to-first-real-
use**. Feed real support tickets back into the guide — the recurring tickets *are* the
missing sections. Add a one-line feedback control to both the PDF and each LMS module.
A/B the quick card against the full guide on real onboarding to see what actually gets used.

---

## 9. Concrete outline for the StudioFire operator guide

```
COVER — StudioFire Operator Guide · <station> · v<app-version> · <date>

PART A — QUICK CARD (1–2 pp, laminate)
  A1  On air in 5 steps                (annotated cockpit screenshot)
  A2  If something goes wrong          (7 symptoms → what to do / who to call)
  A3  The five things to know          (mental model)

PART B — TUTORIAL "Your first shift" (guided, ~10 pp)
  B1  Meet the screen                  (cockpit zones labelled)
  B2  Going on air / off air
  B3  Skipping and stopping safely
  B4  What "Studio health" means
  B5  Reading now-playing and history
  B6  You did it — a normal shift, end to end

PART C — HOW-TO SHEETS (2 pp each, one per task)
  C1  Build or edit a playlist (incl. Zara .lst compatibility)
  C2  Schedule a show (once / daily / weekly; hand-back to rotation)
  C3  Add a spot (folder / interval / :past the hour / manual)
  C4  Add music to the library (and why indexing takes a while)
  C5  Add studio equipment to the monitor
  C6  Run and export an as-aired report (CSV for affidavits)
  C7  Manage users
  C8  What the ⟳ restart button does

PART D — REFERENCE (online-preferred, short in print)
  D1  Glossary
  D2  Troubleshooting by symptom
  D3  Settings quick reference
  D4  Support & escalation, version/regeneration note
```

Each lettered item = one LMS module candidate (Part B = the "onboarding" course;
Part C = the "task skills" course; Part A/D = printable job aids, not assessed).

---

## 10. Sources consulted (2026-09-14)

- **Diátaxis** — *How-to guides* (diataxis.fr/how-to-guides/), *Tutorials*
  (diataxis.fr/tutorials/), *The difference between a tutorial and how-to guide*.
  (Framework for separating learning-oriented tutorials from task-oriented how-tos;
  the "machinery vs. user goal" failure example; narrative-of-the-expected.)
- **Google Technical Writing One** — *Short sentences*
  (developers.google.com/tech-writing/one/short-sentences): one idea per sentence,
  convert embedded lists to bullets, cut filler ("at this point in time"→"now").
- **Write the Docs** — *How to write software documentation*
  (writethedocs.org/guide/writing/beginners-guide-to-docs/): audience split (users vs
  developers), FAQ-as-documentation pitfalls, README template.
- **Wikipedia** — *Instructional design* (ADDIE; Gagné, Mager, Scriven; formative
  evaluation before finalising); *Conditions of Learning* (Gagné's nine events of
  instruction); *Andragogy* (Knowles' six adult-learner assumptions); *E-learning
  (theory)* / multimedia learning (Mayer's cognitive principles); *SCORM* & *Experience
  API/xAPI* (LMS standards, tracking limits, package-size pitfall); *Technology
  Acceptance Model* (perceived usefulness + ease of use drive adoption).
- **Nielsen Norman Group** — *10 Usability Heuristics* (visibility of system status,
  match the user's language, recognition rather than recall, error prevention, help &
  documentation) — used to align guide language with the GUI's status signalling.
- **W3C WAI** — *WCAG 2.2 Understanding SC 1.4.12 Text Spacing* (line height ≥1.5×,
  paragraph ≥2×, letter ≥0.12×, word ≥0.16×; white space helps cognitive access).
- **Wikipedia** — *PDF/UA (ISO 14289)*: tagged PDF, logical reading order, correct
  semantic tags, alt text on meaningful graphics, embedded fonts — the accessible-PDF
  target the guide should meet.

*Web-search backends (Firecrawl) were out of credits at research time; all sources above
were fetched directly (curl / urllib).*

## Related
- [[StudioFire/docs/Home|StudioFire Home]]
- [[reporting-pipeline]] (HTML→PDF rendering, brand constants, jargon-blacklist pattern)
- [[veteranop-document-manager]] (document standards, evaluation)
