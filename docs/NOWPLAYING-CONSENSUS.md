# Now-playing metadata → your stream host — design decisions

What the station's website player shows, and why the implementation looks the way
it does. Written for any station; the specific host, mount and vendor contacts for
one installation live in `site-notes/`.

## The problem

A player reads song titles from the stream's ICY metadata. Two things commonly go
wrong, and they look identical from the web page:

1. the mount advertises `icy-metaint` but publishes an **empty** `StreamTitle`;
2. the encoder reads a now-playing file that nobody is updating.

Both produce "song info blank" while audio plays perfectly.

## Decision

Publish the title in **two independent ways**, and never let either affect audio:

| Path | What it does | Who it is for |
|---|---|---|
| **Now-playing file** | StudioFire writes the current song to a text file on every track change | an encoder (e.g. BUTT) that polls the file |
| **Direct push** | the `nowplaying_push` transport posts the same line to the stream host | hosts that accept metadata over HTTP |

The file path is the default because it works with any encoder and needs no
credentials. The push is opt-in, admin-only, and gated on the host's written
answer about its endpoint.

## Non-negotiable

* **Audio never depends on metadata.** A slow, broken or absent stream host must
  not delay, skip or distort a track. The push runs off the play log, never in the
  playout path.
* **No invented data.** A song with no tag shows what the file says, not a guess.
* **Secrets live outside the repo.** The push credential is read from an
  environment-sourced secret file; it is never written to the database or to git.
* **Transport is pluggable.** `IcecastUpdinfoTransport` is the common shape; a
  host-specific API is another. `DryRunTransport` exists so the wiring can be
  tested end-to-end with nothing to send to — it is a development stand-in, not a
  user-facing state (never show its name in the UI).
* **The ASCII fold stays.** Most hosts mangle UTF-8 in ICY titles, so the line is
  folded to plain ASCII ("Björk" → "Bjork"). That is deliberate.

## Flexible

* The song line format (`{artist} | {title} | {album} | {label} {isrc}` style) is
  configurable, because hosts differ in the field order they parse.
* Whether the push is on at all. Off by default; a station whose encoder watches
  the file never needs it.
* Push interval (the floor is a few seconds, to avoid hammering a host).

## Verification

Before believing a host works, check the listener side, not just our own log:

```bash
curl -s -H "Icy-MetaData: 1" http://STREAM-HOST:PORT/MOUNT \
  | head -c 2000 | strings | grep -i streamtitle
```

An empty `StreamTitle` while audio plays means the host is not publishing what we
sent — a host-side question, not a StudioFire bug.

## What did NOT change

* The engine, the fallback chain and the precache digest are untouched by
  metadata work.
* Reports and the royalty report read `play_history`, not the push, so a broken
  stream host cannot affect a filing.

## Related

* `web/templates/help.html` → "Song titles & streaming" (operator view).
* `services/core/nowplaying_file.py` (file), `services/core/nowplaying_push.py`
  (the push), `services/core/tagfields.py` (which tags feed the line).
