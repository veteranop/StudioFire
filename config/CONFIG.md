# config.json — every setting, in plain English

`config/config.json` is **yours**, not the program's. It is gitignored, it is
created by the setup wizard, and every value in it can also be changed from
**Settings** in the web UI. You never have to edit this file by hand, but you
can — StudioFire validates it on load and shows the error in the UI rather than
failing silently.

> **No config file at all is a valid state.** A fresh install starts with
> defaults, opens the setup wizard, and writes the file when you finish. Nothing
> station-specific ships in the repo.

The complete, commented example is [`config.example.json`](config.example.json).
Copy it to `config.json` only if you would rather start from a file than from the
wizard.

## Top level

| Key | What it means | Default |
|---|---|---|
| `schema_version` | Format version of this file. Leave at `1`. | `1` |
| `station_name` | The name shown in the top bar, page titles and reports. | `StudioFire` |

## `paths` — where things live

| Key | What it means | Default |
|---|---|---|
| `data_root` | Base folder for StudioFire's own data. | `%ProgramData%\StudioFire` |
| `data_dir` | The database lives here. | `<data_root>\data` |
| `logs_dir` | Service and engine logs. | `<data_root>\logs` |
| `precache_dir` | The on-disk copy of upcoming music (this is what makes a dropped share survivable). | `<data_root>\precache` |
| `emergency_dir` | The local fallback audio used when the library vanishes. | `<data_root>\emergency` |
| `nas_music_root` | **Where your music is.** A UNC path (`\\NAS\Music`) or a local folder (`D:\Music`). | empty |
| `path_aliases` | Rewrites paths inside imported `.lst` files, e.g. `{"\\\\OLD-NAS\\music": "Z:"}`. | `{}` |
| `share_aliases` | The drive-letter ↔ share mapping, e.g. `[{"share": "\\\\NAS\\Music", "drive": "Z:"}]`. Filled in by the setup wizard when a folder is picked through a mapped drive. | `[]` |

A mapped drive is **optional**. UNC paths work end to end; the mapping only
exists so old playlists that contain `Z:\...` keep playing.

## `engine` — the audio engine

| Key | What it means | Default |
|---|---|---|
| `audio_device_guid` | Pin the output to one device by GUID. Empty = the Windows default (fine for most stations; a fixed device is better if the PC has several). | empty |
| `wasapi_mode` | `shared` (mix with other apps) or `exclusive`. | `shared` |
| `watchdog_interval_sec` | How often the supervisor checks the engine. | `1` |
| `ipc_host` / `ipc_port` | Where the engine listens for the core. Loopback by default. | `127.0.0.1:7701` |
| `crossfade_sec` | Crossfade length between tracks. | `4` |

## `core` — the web service

| Key | What it means | Default |
|---|---|---|
| `bind_host` | `0.0.0.0` lets other PCs on the LAN open the GUI; `127.0.0.1` keeps it to this machine. | `0.0.0.0` |
| `bind_port` | The web UI port. The installer opens this port in the firewall (LAN profile). | `8080` |
| `precache_target_minutes` | How much upcoming music to keep on local disk. Bigger = more resilient to a share dropout, more disk. | `45` |
| `feed_ahead_tracks` | How far ahead of the play head the engine queue is fed. | `3` |
| `allow_gui_restart` | Shows a "restart everything" button in the UI. Set `false` on an on-air PC so nobody can drop audio from a browser. | `true` |
| `update_check` | Look for a newer release every few hours. | `true` |

## `nowplaying_push` — song titles to your stream host (optional)

The file-based route (`Settings → Streaming → Now-playing file`) needs none of
this. The push is for hosts that accept metadata over HTTP.

| Key | What it means | Default |
|---|---|---|
| `enabled` | Turn the push on. | `false` |
| `transport` | `icecast` (SOURCE/updinfo) or `dryrun` (build the line, send nothing). | `dryrun` |
| `endpoint` | The stream host's address, e.g. `http://stream.example.org:8000` (no path). | empty |
| `mount` | Mount point, e.g. `/live`. | empty |
| `auth_env` | Name of the environment variable/file that holds the credential. **Passwords never go in this file.** | `STUDIOFIRE_STREAM_AUTH` |
| `timeout_sec` | Give up on the host after this long. | `4` |
| `min_interval_sec` | Never push more often than this. | `5` |
| `include_spots` | Also publish spot/ID titles. | `false` |

## `royalty_reporting` — SoundExchange Report of Use

| Key | What it means | Default |
|---|---|---|
| `service_name` | **Required to file.** The name the station files under. There is no default on purpose. | empty |
| `filer_name` | Only if somebody else signs for the station. | empty |
| `contact` | `name`, `street`, `city_state_zip`, `phone`, `email` — needed for the headered layout. | empty |
| `category` | `A` webcast, `B` broadcast simulcast (music), `C` news/talk/sports/business. | `B` |
| `mode` | `census_atp` (monthly, every song), `sample_atp` or `sample_ath` (quarterly, two weeks — eligible minimum-fee webcasters only). | `census_atp` |
| `channel_name` | The channel/program name column in the listening-hours format. Defaults to the service name. | empty |
| `output_format` | `headerless` (data rows only) or `headered` (the 14-line header). | `headerless` |
| `share_aliases` | Share roots to try when resolving a played file, if the library is reached under more than one name. | `[]` |
| `music_drive_letter` | The drive letter (e.g. `Z:`) the library is mapped to, if any. | empty |

## `poller` — optional network monitoring

Used by the station-equipment pinger. Every field is optional; leave arrays empty
if you do not want it.

| Key | What it means |
|---|---|
| `interval_sec` | How often to probe. |
| `unifi.host` / `unifi.api_key` | UniFi controller, if you have one. |
| `barix` | List of Barix encoders/decoders to poll. |
| `tx150.host` / `tx150.port` | Transmitter exciter via a serial gateway, if present. |
| `wireguard_peer_ip` | The far end of a tunnel, if the station uses one. |

## If something is wrong with this file

StudioFire validates on load and reports the problem in the web UI with a link to
Settings, instead of refusing to start. Playout still starts on emergency audio —
a bad config must never mean silence.
