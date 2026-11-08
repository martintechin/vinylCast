# CLAUDE.md – VinylCast conventions

PLAN.md is the source of truth for scope, milestones and acceptance criteria. Read it before working.

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

Slow-client detection depends on server.py limiting per-connection socket buffering
(SO_SNDBUF + transport high-water mark); without it Linux absorbs ~100 s of audio per
stalled connection and the hub never sees the lag. Keep test producers real-time.

Module boundaries matter: revision 1 replaces `cli.py` with a web UI without touching the audio path.
