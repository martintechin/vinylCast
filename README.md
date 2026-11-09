# VinylCast

Turntable → phono preamp → Behringer UFO202 → Raspberry Pi 3 → HTTP audio stream → Google Cast
speaker or group. Drop the needle and the record plays on the last-used speaker; lift it and the
speaker stops after a silence timeout. A phone web page shows what is happening and controls it.

- Scope and history: [PLAN.md](PLAN.md) (proof of concept, M0–M6) and
  [docs/PLAN-rev1.md](docs/PLAN-rev1.md) (revision 1, R0–R5)
- Status summary for the wider project: [docs/STATUS.md](docs/STATUS.md)
- Conventions for working on the code: [CLAUDE.md](CLAUDE.md)

## Status (2026-11-08)

| Stage | Status |
| --- | --- |
| PoC M0–M4 | done |
| PoC M5 soak test | tooling done; the 60-minute run is deferred |
| PoC M6 codec decision | keep both codecs, MP3 320k default |
| Rev 1 R0 prerequisites | done (level measurement, detection threshold, power: `get_throttled` 0x0 after > 24 h) |
| Rev 1 R1 control core (needle drop → cast, silence → stop) | done |
| Rev 1 R2 web API + live events | done |
| Rev 1 R3 phone UI | done (Android Chrome) |
| Rev 1 R4 auto-cast acceptance | next: tooling ready, test runs pending |
| Rev 1 R5 24 h unattended | open |

## Everyday use (on the Pi)

The systemd service runs `vinylcast daemon` with `vinylcast.toml`:

```sh
sudo systemctl start vinylcast        # or: enable it to start at boot (scripts/deploy.sh --enable)
journalctl -u vinylcast -f            # live log
sudo systemctl stop vinylcast         # clean stop: also stops playback on the speaker
```

Phone UI: **`http://<pi-ip>:8800/`** (same Wi-Fi). Status, play/stop, speaker volume, speaker
choice, live input level, and settings behind the cog icon (auto-cast, codec, threshold, silence
timeout). Settings changed there are saved in `state.json` and survive restarts.

Behaviour:

- Music detected at the input (RMS above the threshold for 1.5 s) → cast to the last speaker
- Silence for 180 s → stop; shorter silences (side flips, track gaps) keep playing
- Stopped by hand (page, Google Home app, voice) → no auto-cast until the record has ended
- Speaker busy with another app → not taken over; shown on the page

## Install / update on the Pi

```sh
git clone <repo> ~/vinylcast && cd ~/vinylcast
scripts/deploy.sh          # apt ffmpeg/alsa-utils if missing, venv + install, vinylcast.toml,
                           # systemd unit; restarts the service if it is running
scripts/deploy.sh --enable # also start at boot
```

`deploy.sh` runs on the Pi itself (`git pull` unless `--no-pull`). Commands outside the service
need the venv: `. .venv/bin/activate` (or call `.venv/bin/vinylcast …`).

## Configuration

`vinylcast.toml` (created from `config.example.toml`; every key is documented there). Order of
precedence: built-in defaults < `vinylcast.toml` < `state.json` (settings changed in the UI) <
command-line flags (`sample_rate` → `--sample-rate`, `cast_target` → `--to`).

Keys worth knowing:

| Key | Default | Notes |
| --- | --- | --- |
| `source` | `alsa` | `alsa` (UFO202), `tone` (test tone), `file` (`source_file`) |
| `alsa_device` | `hw:CARD=CODEC,DEV=0` | `hw:` so an unsupported rate fails instead of being resampled |
| `codec`, `mp3_bitrate` | `mp3`, `320k` | FLAC is the alternative; UI-changeable |
| `cast_target` | none | Fallback speaker until one is chosen in the UI |
| `auto_cast` | `true` | Needle-drop casting; UI-changeable |
| `detect_threshold_db` | `-40.0` | Music threshold (RMS dBFS), from the R0 noise-floor measurement |
| `stop_after_silence_s` | `180` | Longer than a side flip |
| `measure_levels`, `peak_log_s` | `true`, `10` | Level measurement in ffmpeg; peak logged every 10 s |
| `metrics_path` | `./metrics.jsonl` | JSONL event log; strftime codes allowed |
| `log_level` | `info` | `debug` adds routine events (input_peak, http_request) to the console |

## Commands

| Command | Does |
| --- | --- |
| `daemon` | Always on: detection, auto-cast, phone UI and API (what the service runs) |
| `levels [--duration N]` | Live input peak/RMS per channel; service must be stopped (input is exclusive) |
| `devices` | List Cast devices and groups |
| `serve` | Stream only (no Cast) |
| `cast --to NAME` / `stop --to NAME` | Send the stream to a speaker / stop it (needs a running `serve`) |
| `run --to NAME` | `serve` + `cast` in one process (PoC soak entry point) |

Scripts:

| Script | Does |
| --- | --- |
| `scripts/deploy.sh` | Install/update on the Pi, systemd unit |
| `scripts/soak_summary.py FILE` | Soak verdict (PLAN.md M5 criteria, CPU, Wi-Fi, USB loss, peaks) |
| `scripts/r4_summary.py FILE [--idle-from HH:MM --idle-to HH:MM]` | Needle-drop cycles and R4 criteria |
| `scripts/spike_measure.py [--noise-floor]` | ffmpeg measurement variants / guided noise-floor measurement |
| `scripts/bench_framing.py N` | Framing CPU cost per codec |

## HTTP API (port 8800, LAN only, no authentication in revision 1)

| Endpoint | Does |
| --- | --- |
| `GET /` | Phone UI |
| `GET /stream.mp3`, `/stream.flac` | The audio stream (only the active codec answers) |
| `GET /api/status` | State, speaker, signal, level, volume, codec, notifications |
| `GET /api/devices` | Live device and group list |
| `POST /api/play {"target"?}` / `POST /api/stop` | Manual play (or switch) / stop |
| `POST /api/target {"target"}` | Choose the speaker for the next needle drop |
| `POST /api/volume {"level": 0..1}` | Speaker volume (while playing) |
| `GET`/`POST /api/settings` | UI-changeable settings |
| `GET /api/diagnostics` | Wi-Fi, throttling, CPU, recent events |
| `GET /api/events` | Server-Sent Events: `status` on change, `level` at 4 Hz |
| `GET /health` | Encoder and stream status |

JSON bodies must be UTF-8 (Windows PowerShell 5.1: send `[Text.Encoding]::UTF8.GetBytes(...)`).

## Development (no hardware needed)

Python 3.11+ and ffmpeg.

```sh
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
ruff check . && ruff format --check .
pytest -q                                          # 187 tests
vinylcast daemon --source tone --to "Any" --http-port 8800   # UI at http://localhost:8800/
vinylcast serve --source tone && ffplay http://localhost:8800/stream.mp3
```

## Codec decision (M6)

**Both codecs stay; MP3 320k is the default** (Martin, 2026-11-08). FLAC is a setting; switching
in the UI restarts the stream in-process and re-casts (~2 s of silence on the Pi).

| Codec | Speaker | Result | Startup | Notes |
| --- | --- | --- | --- | --- |
| MP3 320k | Tvättstugan (Nest Mini) | plays | 2.5–3.6 s | |
| MP3 320k | HESTON 120 | plays | 3.0 s | one 0.5 s rebuffer right after start (accepted) |
| FLAC | Tvättstugan (Nest Mini) | plays | 3.9 s | survives an encoder restart |
| FLAC | HESTON 120 | plays | 1.4 s | same startup rebuffer |
| either | Cast group | not tested | | no group yet |

On the Pi 3: MP3 320k encoding ~29 % of one core, level measurement +1.5 %, framing 0.1–0.3 %.

## Open items

| Item | Why open | How to close |
| --- | --- | --- |
| R4 auto-cast acceptance | Test runs pending | Play records over time with `metrics_path = "./r4.jsonl"`; then `python scripts/r4_summary.py r4.jsonl --idle-from HH:MM --idle-to HH:MM` |
| PoC M5 60-minute soak | Deferred by Martin; listening so far has been good | 62+ min of play, then `python scripts/soak_summary.py FILE`; also covered by R5's 24 h test |
| Cast group checks | No speaker group exists yet | Create one in Google Home; play to it from the UI |
| Loudest-passage level check | No loud record at hand; loudest seen −9.9 dBFS | `vinylcast levels` on a loud record (service stopped) |
| End-to-end latency ~2 s | Parked: Pi share ≤ ~0.2 s, the rest is the speaker's buffer | Measure needle lift → silence per codec × speaker if it becomes important |
| Wi-Fi power save after reboot | Set via nmcli on the netplan connection | `iw dev wlan0 get power_save` after a reboot |
