# VinylCast

PoC: turntable → phono preamp → Behringer UFO202 → Raspberry Pi 3 → HTTP audio stream → Chromecast device or Cast group.
See [PLAN.md](PLAN.md) for scope, milestones and acceptance criteria.

Status: **M5 code done; the 60-minute soak run is DEFERRED** (Martin, 2026-11-08: no
indication of problems so far, listening has been good). Open items are listed under
"Deferred" below. M4 passed (loudest-passage
check on available records only: -10.3 dBFS; re-check with a loud record). M3 passed on a
single device (Nest Mini); group checks open (no group yet).

## Soak test

```sh
# vinylcast.toml: source, codec, cast_target, metrics_path = "./soak-%Y%m%d-%H%M.jsonl"
sudo systemctl start vinylcast        # play for 60+ min; note audible gaps with times
sudo systemctl stop vinylcast
python scripts/soak_summary.py soak-*.jsonl   # PASS/FAIL against PLAN.md M5
```

## On the Pi

```sh
scripts/deploy.sh                 # git pull, venv + install, vinylcast.toml, systemd unit
vinylcast levels                  # live dBFS from the UFO202; keep peaks below -1 dBFS
vinylcast run --to "<device>"     # vinyl to Cast (source defaults to alsa)
sudo systemctl start vinylcast    # unattended, using vinylcast.toml (needs cast_target)
journalctl -u vinylcast -f
``` `serve` streams MP3 or FLAC from the
`tone`, `file` or `alsa` source; `devices`, `cast`, `stop` and `run` control Cast devices and groups.

## Quick start (no hardware)

```sh
vinylcast serve --source tone          # prints the stream URL
ffplay http://<ip>:8800/stream.mp3     # or open the URL in VLC
curl http://<ip>:8800/health
```

Events and 5 s samples go to `./metrics.jsonl` (`--metrics-path` to change).

## Development

Requires Python 3.11+ and ffmpeg (`apt install ffmpeg alsa-utils` on the Pi).

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
python -m vinylcast --help
ruff check . && ruff format --check .
pytest
```

## Configuration

Copy `config.example.toml` to `vinylcast.toml` and pass `--config vinylcast.toml`.
`tone_beep_s` (not in the original plan) switches the tone source to a 0.1 s beep every N
seconds for latency checks. `source_file` is the path used by the `file` source.

Every key can be overridden with a flag (`sample_rate` → `--sample-rate`); `cast_target` is `--to`.
Precedence: built-in defaults < TOML < flags.

## Commands

| Command | Does | Milestone |
| --- | --- | --- |
| `devices` | Lists Cast devices and groups | M3 |
| `levels [--duration N]` | Peak/RMS dBFS per channel once per second | M4 |
| `serve` | Encoder + hub + HTTP server | M1 |
| `cast --to NAME` | Sends the stream URL to a target | M3 |
| `stop --to NAME` | Stops playback on a target | M3 |
| `run --to NAME` | `serve` + `cast` in one process | M3 |

## Deferred / open items

| Item | Why open | How to close |
| --- | --- | --- |
| M5 60-minute soak (MP3, one device) | Deferred by Martin; short runs on Nest Mini and HESTON 120 sounded fine | `sudo systemctl start vinylcast` for 62+ min, then `python scripts/soak_summary.py $(ls -t soak-*.jsonl \| head -1)` |
| Cast group checks (M3, M6) | No speaker group exists yet | Create a group in Google Home, rerun `devices` and `run --to <group>` |
| Loudest-passage level check (M4) | No loud record at hand; loudest seen -10.3 dBFS | `vinylcast levels` on a loud record (service stopped) |
| Pi under-voltage | `get_throttled` 0x50000 on 2026-11-08: under-voltage and throttling have occurred since boot (not at that moment) | Official 5.1 V / 2.5 A supply and cable; after a reboot `vcgencmd get_throttled` must stay 0x0 (`journalctl -k \| grep -i voltage` shows when) |
| Startup rebuffer on HESTON 120 | Explained: always one 0.5 s BUFFERING ~0.5 s after the first PLAYING (MP3/FLAC, with/without measurement); none afterwards in a 3.4 min run | Accepted (Martin): soak summary gives a 5 s startup grace and reports it as `startup_rebuffers` |
| End-to-end latency ~2 s (needle lift → silence) | Martin, 2026-11-08; parked. Estimated Pi share ≤ ~0.2 s (capture, encoder, framing holds one frame); the rest is the speaker's own buffer, not controllable with the default Cast receiver | Measure 5 lifts per codec × speaker: if FLAC is clearly faster the receiver buffers by bytes (argues for FLAC default + smaller FLAC blocks, ~0.15 s); else ~2 s is the floor. A custom Cast receiver app is the only direct lever (large, uncertain on audio-only speakers). Sending history to new clients would ADD latency |
| Wi-Fi power save off after reboot | Set via nmcli on the netplan connection; persistence unverified | `iw dev wlan0 get power_save` after a reboot |

## Codec decision (M6)

**Decided (Martin, 2026-11-08): keep both codecs, MP3 320k is the default.** PLAN.md framed M6
as FLAC *or* MP3 for revision 1; instead both stay, and M6 only decides the default. FLAC is a
setting (`codec = "flac"` in vinylcast.toml, or `--codec flac`); changing it needs a service
restart (the receiver reconnects, ~3-4 s of silence). Whether FLAC becomes the default is
decided by a FLAC soak / codec comparison later.

Evidence so far (short runs, no soak):

| Codec | Target | Result | CPU | Startup | Notes |
| --- | --- | --- | --- | --- | --- |
| MP3 320k | Tvättstugan (Nest Mini) | plays (tone, vinyl) | not measured | 2.5-3.6 s | M3/M4; service start/stop clean |
| MP3 320k | HESTON 120 | plays (vinyl) | not measured | not measured | Cast Lite, same HTTP behaviour |
| FLAC | Tvättstugan (Nest Mini) | plays (tone) | not measured | 3.9 s | survives encoder restart (frame numbers reset) |
| FLAC | HESTON 120 | plays (vinyl) | not measured | 1.37 s | one 0.5 s BUFFERING right after the first PLAYING; recurrence unknown |
| either | Cast group | not tested | | | no group yet |

Framing CPU on the Pi 3 (bench_framing.py, 20 s noise): MP3 0.13 %, FLAC 0.25 % of one core.
