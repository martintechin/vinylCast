# VinylCast – software status (2026-11-08)

Handover summary for the overall project (hardware, enclosure, product planning). Covers the
code only. Source of truth for scope: `PLAN.md`; conventions: `CLAUDE.md`; open items:
`README.md` → "Deferred / open items". Repository `martintechin/vinylcast`, branch
`claude/new-session-jyysa7` (each milestone commit body contains its test evidence).

## 1. What works today

The full chain runs on the existing hardware and has been listened to on two speakers:

```
turntable → own phono preamp → Behringer UFO202 (USB, line in) → Raspberry Pi 3 Model B
  → ffmpeg (capture + encode) → Python HTTP server → Wi-Fi → Cast speaker pulls the stream
```

- One Python service (`vinylcast`) with a CLI: `devices`, `levels`, `serve`, `cast`, `stop`, `run`.
- Codecs: **MP3 320 kbit/s (default)** and **FLAC 16-bit**; both kept, selectable by config
  (change = service restart).
- Runs unattended as a systemd service (`vinylcast run` with `vinylcast.toml`), clean start/stop
  (stop also stops playback on the speaker).
- Speakers verified: **"Tvättstugan"** (Google Nest Mini) and **"HESTON 120"**, both with MP3
  and FLAC (short runs). No Cast *group* tested yet (none exists).
- Startup (play command → speaker playing): 2.5–3.9 s on the Nest Mini, 1.4 s on HESTON 120.
- CPU on the Pi 3: stream framing costs 0.13 % (MP3) / 0.25 % (FLAC) of one core; total
  service CPU under real load not yet measured over a long run (soak deferred).

## 2. Milestones (PLAN.md)

| Milestone | Status |
| --- | --- |
| M0 skeleton, config, CLI | done |
| M1 encode + serve (MP3), multi-client, slow-client drop, encoder restart | done |
| M2 framing (MP3/FLAC frame boundaries, FLAC header), mid-stream join | done |
| M3 Cast discovery/play/stop/status logging | done for single devices; **group open** |
| M4 Pi: ALSA input, `levels`, systemd, deploy | done (loudest-passage check only on available records: −10.3 dBFS peak) |
| M5 soak test tooling (metrics, `soak_summary.py`) | code done; **60-min run deferred** |
| M6 codec decision | **decided: keep both, MP3 default** (plan said either/or) |

Tests: 119 automated tests (pytest), all passing; hardware/Cast checks were run manually on
the Pi.

## 3. Architecture (for anyone extending it)

- ffmpeg does all audio work (capture, encode). Python only moves encoded bytes and controls
  Cast; it never decodes or resamples.
- One encoder, one fan-out hub: every additional listener costs a queue, not another encode.
- The speaker **pulls** an HTTP stream (`http://<pi-ip>:8800/stream.mp3` or `.flac`);
  the Pi only sends it the URL (pychromecast). `/health` returns JSON status.
- Module boundaries: `source`, `encoder`, `framing`, `hub`, `server`, `cast`, `metrics`,
  wired in `service.py`; `cli.py` is a thin front end so revision 1 can add a web UI
  without touching the audio path.
- Metrics: JSONL event log + 5 s CPU/memory samples + 60 s Pi health (throttling, Wi-Fi
  signal); `scripts/soak_summary.py` turns a run into PASS/FAIL against the plan.

## 4. Measured hardware facts (relevant for custom hardware)

| Item | Measured | Consequence |
| --- | --- | --- |
| Board | Raspberry Pi 3 **Model B** Rev 1.2 | Wi-Fi **2.4 GHz only** |
| Network | Wi-Fi only at the turntable (no Ethernet); ch. 11, signal **−73 dBm** (marginal), 58–72 Mbit/s | Bandwidth ample (MP3 0.32, FLAC ~1 Mbit/s); weak signal is a dropout risk. Wi-Fi power save disabled. |
| Audio interface | UFO202 = TI/Burr-Brown PCM2902, **USB full speed (1.1)**, capture **16-bit stereo** at 48/44.1/32/22.05/16/11.025 kHz, **no input gain** | Level set only at the preamp; 16-bit ceiling; streaming at 44.1 kHz |
| USB | Kernel reported lost capture transfers ("FIQ reported NYET. Data may have been lost") | Known Pi 3 weakness with full-speed devices on its shared USB bus; a design with a native I2S ADC or a USB-2.0 high-speed interface avoids it |
| Power | `get_throttled` = 0x50000: **under-voltage and throttling have occurred** since boot | Power supply/cable inadequate for Pi 3 + USB audio; hardware design needs margin here |
| Input level | Loudest seen −10.3 dBFS peak, RMS ≈ −26 dBFS | ~9 dB headroom with current preamp setting |
| Input exclusivity | ALSA `hw:` device: one program at a time | `levels` cannot run while the service runs |
| ffmpeg version | Pi OS ships ffmpeg 7.x | An ffmpeg-7-specific filter-graph bug blocks in-encoder peak logging (feature disabled) |

## 5. Receiver behaviour observed (both speakers identical)

`User-Agent: Cast Lite`, `GET` only over HTTP/1.1 keep-alive, no `HEAD`, no `Range`,
no ICY metadata; accepts an endless chunked stream for MP3 and FLAC. A FLAC encoder restart
(ffmpeg killed) was bridged without interruption on the Nest Mini. HESTON 120 showed one
0.5 s BUFFERING right after starting FLAC playback (recurrence unknown).

## 6. Decisions and deviations from PLAN.md

- Pi is the dev box (Martin's desktop is Windows); `deploy.sh` installs/updates on the Pi
  itself instead of rsync from a dev machine.
- Default input device `hw:` instead of `plughw:` (unsupported rates fail instead of being
  resampled by ALSA).
- Soak test on Wi-Fi with diagnostics instead of Ethernet-first (no Ethernet available).
- Both codecs kept; MP3 default.
- Peak-level logging during streaming: built, but off by default (breaks the stream with
  ffmpeg 7).

## 7. Open items (detail in README)

1. 60-minute soak test (MP3, one speaker) — deferred.
2. Cast group test (needs a group in Google Home).
3. Power supply: fix under-voltage, then confirm `get_throttled` stays 0x0.
4. Early BUFFERING on HESTON 120 with FLAC — check if it recurs; possible fix (send new
   listeners a few seconds of recent audio) contradicts PLAN.md and needs approval.
5. Loud-record level check; Wi-Fi power-save persistence after reboot; peak logging on ffmpeg 7.

## 8. Not built (out of PoC scope)

Web UI, auto-cast to last speaker, start on needle drop, phono stage, Home Assistant,
Wi-Fi provisioning, authentication, enclosure/PCB, licence. Revision 1 (phone web UI +
auto-cast) is the next planned software stage and still needs its own plan.
