# VinylCast

PoC: turntable → phono preamp → Behringer UFO202 → Raspberry Pi 3 → HTTP audio stream → Chromecast device or Cast group.
See [PLAN.md](PLAN.md) for scope, milestones and acceptance criteria.

Status: **M0 (skeleton)** — CLI and config only; subcommands are stubs.

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
Every key can be overridden with a flag (`sample_rate` → `--sample-rate`); `cast_target` is `--to`.
Precedence: built-in defaults < TOML < flags.

## Commands

| Command | Does | Milestone |
| --- | --- | --- |
| `devices` | Lists Cast devices and groups | M3 |
| `levels` | Peak/RMS dBFS once per second | M4 |
| `serve` | Encoder + hub + HTTP server | M1 |
| `cast --to NAME` | Sends the stream URL to a target | M3 |
| `stop --to NAME` | Stops playback on a target | M3 |
| `run --to NAME` | `serve` + `cast` in one process | M3 |

## Results (M6)

To be filled in: codec × target × pass/fail × CPU × startup latency × notes.
