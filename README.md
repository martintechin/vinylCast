# VinylCast

PoC: turntable → phono preamp → Behringer UFO202 → Raspberry Pi 3 → HTTP audio stream → Chromecast device or Cast group.
See [PLAN.md](PLAN.md) for scope, milestones and acceptance criteria.

Status: **M4 (on the Pi), code done, hardware acceptance pending.** M3 passed on a single
device (Nest Mini); group checks open (no group yet).

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

## Results (M6)

To be filled in: codec × target × pass/fail × CPU × startup latency × notes.
