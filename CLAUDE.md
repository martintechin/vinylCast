# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

VinylCast PoC: turntable → UFO202 → Raspberry Pi 3 → HTTP audio stream → Chromecast device or group.
PLAN.md is the source of truth for scope, milestones (M0–M6) and acceptance criteria. Read it before
working. Check `git log` for which milestone is done; each milestone commit body holds its evidence.

## Commands
Dev and run box is the Raspberry Pi (Martin's desktop is Windows; it is only used as a player:
VLC / ffplay / Edge on `http://<pi-ip>:8800/stream.mp3`). This cloud container also runs Linux.
```sh
python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'   # needs ffmpeg on PATH
ruff check . && ruff format --check .          # lint + format check (ruff format . to fix)
pytest -q                                      # full suite, ~12 s; ffmpeg tests skip without ffmpeg
pytest tests/test_hub.py::test_fan_out         # single test
pytest -k slow                                 # by name
vinylcast serve --source tone                  # dev-machine stream, no hardware; prints URL
vinylcast serve --source tone --codec flac     # FLAC variant
vinylcast levels --source tone --duration 3    # level meter (astats); on the Pi: UFO202
scripts/deploy.sh                              # ON THE PI: pull, install, systemd unit
python -m vinylcast --help                     # all subcommands; unimplemented ones exit 3
python scripts/bench_framing.py 20             # framing CPU cost per codec
python scripts/soak_summary.py FILE.jsonl      # soak verdict (logic in vinylcast/soak.py)
tests/fixtures/make_fixtures.sh                # regenerate framing fixtures (ffmpeg)
```

## Working rules
- Build one milestone at a time (M0 → M6). After each: run its checks, report commands and output, stop and wait for approval.
- One commit per milestone; acceptance evidence (commands + output) goes in the commit body.
- Nothing from PLAN.md's "Not in the PoC" list (web UI, auto-cast, auth, HA, Wi-Fi provisioning, …). If a milestone seems to need it, stop and ask.
- Anything needing the Pi, the UFO202 or a Cast device: write the exact commands for Martin to run and ask for the output. Never assume the result.

## Code
- Python 3.11+, asyncio throughout, type hints everywhere. `ruff check`, `ruff format`, `pytest` must pass.
- Runtime dependencies: aiohttp, pychromecast, psutil only (pinned `==` in pyproject.toml). Ask before adding any other dependency, including dev ones. Dev dependencies approved so far: pytest, ruff, pytest-asyncio (async tests and fixtures; aiohttp's own pytest plugin is deprecated and not used).
- Don't write aiohttp or pychromecast calls from memory: read the installed version's source (`.venv/lib/python*/site-packages/...`) first. If you bump a pin, re-verify.
- Audio processing stays in ffmpeg. Python never decodes, resamples or mixes audio; it only moves encoded bytes and controls Cast.
- Tests that need hardware or Cast devices never run automatically. Integration tests use the `tone` source and ffmpeg decode checks.

## Cross-file conventions
- Config keys live in four places that must stay in sync: `Config` + `_TYPES` in config.py,
  `CONFIG_KEYS` + an override flag in cli.py, and config.example.toml (a test asserts the example
  file equals the defaults). Precedence: defaults < TOML < explicitly passed flags. `cast_target` is `--to`.
- `Hub.publish(data, duration_s)` is synchronous and its queue limit is in seconds of audio, so every
  producer must tag items with their true duration; framing provides it per frame.
- service.py is the only place the audio path is wired: encoder `sink` -> framer.feed -> hub.publish.
  The encoder's `on_start` hook resets the framer for each new ffmpeg process; for FLAC the
  framer re-captures the stream header and service compares it (changed -> all clients dropped).
  The server writes `framer.stream_header` to a new client right before its first frame.
- Known open issue: after an encoder restart FLAC frame numbers restart at 0, so connected FLAC
  clients see timestamps jump backwards (audio decodes; see tests/test_stream_e2e.py).
- Every observable event goes through `Metrics.event(name, **fields)` into JSONL (`kind` = event |
  sample). Event names (`encoder_restart`, `client_slow`, `client_disconnect` with `reason`,
  `http_request` with all headers, ...) are what tests and the M5 soak summary read, so don't rename them.
- Tests: pytest-asyncio in auto mode (plain `async def test_...`). tests/conftest.py provides
  `events(path, name)`, `wait_until(cond)` and `requires_ffmpeg`. Encoder/service tests take short
  `backoff_s` / `stall_timeout_s` overrides instead of sleeping through real delays.

## Architecture
The receiver pulls; the Pi never pushes. `cast.py` only hands the receiver a URL; from then on the server serves it like any other HTTP client.

```
audio path (one encoder, one hub; a new client costs a queue, not an encode)

  source.py   builds ffmpeg input args (alsa | tone | file)
      |
  encoder.py  one ffmpeg process -> stdout chunks; stderr -> metrics; restart w/ backoff
      |
  framing.py  MP3 / FLAC frame boundaries, cached FLAC header (pure functions)
      |
  hub.py      one producer -> N bounded per-client frame queues; slow client dropped
      |
  server.py   aiohttp GET/HEAD /stream.mp3 | /stream.flac, /health; logs all requests
      ^
      |  HTTP pull
  Cast receiver  <--- play_media(url with Pi's LAN IP) ---  cast.py

control: cli.py -> service.py (wires encoder -> hub -> server + sampler) -> config.py
metrics.py records events + 5 s samples (JSONL)
```

cast.py wraps pychromecast 14.1.1, which is synchronous and threaded: blocking calls go through
`asyncio.to_thread`, listener callbacks arrive on pychromecast threads (Metrics is locked for this).
`cast` and `stop` are separate short-lived processes appending to the same metrics file; `run` is
serve + cast in one process and the soak-test entry point. Cast events: `cast_state` (every player
state change, with `after_first_playing`), `cast_startup` (latency), `cast_connection`, errors.

UFO202 (measured): card CODEC, USB full speed, native capture S16_LE stereo at 48000/44100/32000/
22050/16000/11025 Hz. Default device is `hw:CARD=CODEC,DEV=0` (not plughw) so an unsupported
rate fails instead of being resampled by ALSA. The systemd unit (deploy/vinylcast.service, a
template filled by deploy.sh) runs `vinylcast run --config vinylcast.toml` and stops with SIGINT.

Pi (measured): Raspberry Pi 3 Model B Rev 1.2 -> Wi-Fi 2.4 GHz only. It runs on Wi-Fi (no
Ethernet at the turntable; Martin's decision, deviating from PLAN.md's Ethernet-first soak), so
M5 logs `health` records every 60 s (get_throttled, Wi-Fi signal/bitrate/power save) and the
summary counts kernel "Data may have been lost" USB messages, to separate USB-capture from
Wi-Fi problems. The encoder's ffmpeg also measures input peaks in an asplit branch
(`peak_log_s`, `input_peak` events) and logs at `level+info`: only warning/error lines become
`ffmpeg_warning`/`ffmpeg_xrun`; info lines go to `on_info`; stderr is ignored once we stop it.
ffmpeg runs in its own session and is stopped SIGTERM -> SIGKILL (clean ALSA close).

Observed receiver behaviour (Nest Mini, "Cast Lite", M3): GET only, HTTP/1.1 keep-alive, no Range,
no HEAD, no Icy-MetaData; sends Accept-Encoding gzip/deflate, so never enable compression. Chunked
endless MP3 and FLAC both play; startup ~3.5-3.9 s. A FLAC encoder restart (frame numbers reset)
is bridged without BUFFERING/IDLE on a single device; groups not yet tested.

Slow-client detection depends on server.py limiting per-connection socket buffering
(SO_SNDBUF + transport high-water mark); without it Linux absorbs ~100 s of audio per
stalled connection and the hub never sees the lag. Keep test producers real-time.

Module boundaries matter: revision 1 replaces `cli.py` with a web UI without touching the audio path.
