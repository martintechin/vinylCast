# VinylCast – software status (2026-11-08)

Handover summary for the overall project (hardware, enclosure, product planning). Covers the
code only. Scope: `PLAN.md` (proof of concept) and `docs/PLAN-rev1.md` (revision 1);
conventions: `CLAUDE.md`; operation and open items: `README.md`. Repository
`martintechin/vinylcast`, branch `claude/new-session-jyysa7` (each milestone commit body
contains its test evidence).

## 1. What works today

The full chain runs on the existing hardware as an appliance:

```
turntable → own phono preamp → Behringer UFO202 (USB, line in) → Raspberry Pi 3 Model B
  → ffmpeg (capture + level measurement + encode) → Python HTTP server → Wi-Fi
  → Cast speaker pulls the stream
```

- **Auto-cast:** the Pi listens all the time. Music at the input (RMS above −40 dBFS for 1.5 s)
  starts playback on the last-used speaker, about 5 s from needle drop to sound. 180 s of silence
  stops it; side flips and track gaps do not. A manual stop (phone, Google Home app, voice) is
  respected until the record has ended; a speaker busy with another app is never taken over.
- **Phone UI** at `http://<pi>:8800/`: status, play/stop, speaker volume (read from the speaker),
  speaker choice, live input meter with clipping warning, settings sheet (auto-cast, codec,
  threshold, timings), diagnostics (Wi-Fi, throttling, CPU, events). Live via Server-Sent Events,
  no external resources, accepted on Android Chrome.
- **HTTP API** (JSON + SSE) behind the UI; LAN only, no authentication in revision 1.
- **Codecs:** MP3 320 kbit/s (default) and FLAC 16-bit; switchable while playing (~2 s gap).
- **Runs as a systemd service** (`vinylcast daemon`), clean start/stop; settings and the last
  speaker persist across restarts (`state.json`).
- **Speakers verified:** "Tvättstugan" (Google Nest Mini) and "HESTON 120", MP3 and FLAC. No
  Cast group tested yet (none exists).
- **Tests:** 187 automated tests (pytest, no hardware needed); hardware and speaker checks were
  run by Martin on the Pi and documented per milestone.

## 2. Milestones

| Stage | Status |
| --- | --- |
| PoC M0–M4: skeleton, encode/serve, framing, Cast control, Pi + ALSA + systemd | done |
| PoC M5: soak tooling | done; the 60-min run is deferred (covered later by R5's 24 h test) |
| PoC M6: codec decision | keep both, MP3 default |
| R0: prerequisites (power, level measurement on ffmpeg 7, noise floor) | done; 24 h power check open |
| R1: control core (state machine, detector, persistence, `daemon`) | done |
| R2: web API + live events | done |
| R3: phone UI | done |
| R4: auto-cast acceptance (10 needle drops, side flip, manual stop, 2 h idle) | tooling ready; runs pending |
| R5: 24 h unattended incl. recovery (speaker/Wi-Fi/Pi restart) | open |

## 3. Architecture (for anyone extending it)

- ffmpeg does all audio work (capture, level measurement, encoding). Python moves encoded bytes,
  parses ffmpeg's level numbers and controls Cast; it never decodes or resamples audio.
- One encoder, one fan-out hub: every listener costs a queue, not another encode.
- The speaker **pulls** an HTTP stream (`/stream.mp3` or `/stream.flac`); the Pi only hands it
  the URL (pychromecast).
- Revision 1 control: level readings → `detector` (on/off with hysteresis) → `controller` (pure
  state machine, one test per rule) → `driver` (executes Cast actions one at a time, persists
  state) → `cast` (pychromecast). `web` serves the API and UI on the same aiohttp server.
- The capture source is isolated in `source.py`: a different ADC (e.g. I²S on custom hardware)
  only changes the ffmpeg input arguments and `alsa_device`.

## 4. Measured hardware facts (relevant for custom hardware)

| Item | Measured | Consequence |
| --- | --- | --- |
| Board | Raspberry Pi 3 **Model B** Rev 1.2 | Wi-Fi **2.4 GHz only** |
| CPU load | MP3 320k encode ~29 % of one core; level measurement +1.5 %; Python ~7 % | Fine on 4 cores; FLAC would be cheaper to encode |
| Network | Wi-Fi only at the turntable; −73 dBm (ch. 11), later −69 dBm (ch. 6); 58–72 Mbit/s | Bandwidth ample (MP3 0.32, FLAC ~1 Mbit/s); weak signal is the main dropout risk. Power save disabled |
| Audio interface | UFO202 = TI/Burr-Brown PCM2902, **USB full speed (1.1)**, capture **16-bit stereo**, 48/44.1/32/22.05/16/11.025 kHz, **no input gain** | Level set only at the preamp; 16-bit ceiling; 44.1 kHz used |
| USB | Kernel reported lost capture transfers ("FIQ reported NYET. Data may have been lost") | Known Pi 3 weakness with full-speed devices on its shared USB bus; a native I²S ADC or a USB 2.0 high-speed interface avoids it |
| Power | `get_throttled` 0x50000 (under-voltage and throttling since boot) before a power-supply change; 0 in later runs | Pi 3 + USB audio needs a solid 5.1 V / 2.5 A supply; design in margin |
| Input level | Music RMS −34 … −25 dBFS, peaks up to −9.9 dBFS | ~9 dB headroom with the current preamp setting |
| Noise floor | Stylus up −64 … −55 dBFS; groove noise (lead-in, run-out) up to −46 … −50 dBFS | Detection: on at −40, off below −46 dBFS |
| Input exclusivity | ALSA `hw:` device: one program at a time | `levels` cannot run while the service runs |
| ffmpeg 7 (Pi OS) | A measuring `asplit` branch with long blocks stalls the stream | Measurement runs inline in 100 ms blocks instead |

## 5. Receiver behaviour observed (both speakers)

`User-Agent: Cast Lite`, `GET` only over HTTP/1.1 keep-alive, no `HEAD`, no `Range`, no ICY
metadata; accepts endless chunked MP3 and FLAC. Startup 1.4–3.9 s depending on speaker and codec.
HESTON 120 always rebuffers once for ~0.5 s right after starting (accepted; the soak summary has
a 5 s startup grace). A FLAC encoder restart was bridged without interruption. End-to-end latency
(needle lift → silence) ~2 s, mostly the speaker's own buffer.

## 6. Decisions and deviations from the plans

- The Pi is the dev box (Martin's desktop is Windows); `deploy.sh` installs on the Pi itself.
- Input device `hw:` instead of `plughw:`.
- Soak on Wi-Fi with diagnostics instead of Ethernet-first (no Ethernet at the turntable).
- Both codecs kept, MP3 default (the plan said either/or).
- Level measurement inline in the encoder (R0 spike on the Pi); peak logging every 10 s.
- Soak criterion "no BUFFERING after first PLAYING" gets a 5 s startup grace.
- Revision 1 decisions: 180 s silence timeout, English UI, no authentication (device-code login
  is a candidate for a later revision).

## 7. Open items (detail in README)

1. R4 auto-cast acceptance runs; R5 24 h unattended test with recovery.
2. Power: confirm `get_throttled` stays 0x0 over 24 h.
3. Cast group test (needs a group in Google Home).
4. Loud-record level check; Wi-Fi power-save persistence after reboot.
5. End-to-end latency ~2 s, parked.

## 8. Not built

Home Assistant, Wi-Fi provisioning, authentication, phono stage, enclosure/PCB, custom Cast
receiver app, licence decision.
