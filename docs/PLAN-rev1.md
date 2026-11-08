# VinylCast Revision 1 – Build Plan

Nov 8, 2026 · draft by Claude Code for @Martin · builds on the PoC (`PLAN.md`, status in
`docs/STATUS.md`)

## Goal

Turn the PoC into an appliance on the existing hardware: drop the needle and the record plays on
the last-used speaker; lift it and the speaker stops after a while. A phone web page shows what
is happening and controls everything else: speaker, play/stop, levels, settings, diagnostics.

Code quality: production-minded where it touches the always-on path (state machine, recovery,
persistence), PoC-grade is fine for the UI's looks. Module boundaries stay as in the PoC; the
audio path (source → encoder → framing → hub → server) is not restructured.

## Scope

Decided by Martin (2026-11-08):

- **Auto-cast = needle drop + last speaker.** The service always captures. When music is detected
  at the input it casts to the last-used speaker; after a configurable silence it stops.
- **Phone web UI:** pick speaker and play/stop; status and live levels; settings; diagnostics.
- **Hardware:** current Raspberry Pi 3 Model B + UFO202. Capture stays behind `source.py`, so a
  later board (e.g. I²S ADC) only changes capture configuration.

In scope:

- Persistent state: last speaker, settings changed in the UI (survive restarts and reboots)
- A control core (state machine) shared by CLI and web UI
- Continuous Cast discovery (live device list incl. groups), volume
- Signal detection (needle drop / silence) and the auto-cast rules below
- HTTP JSON API + Server-Sent Events for live status and levels, on the existing aiohttp server
- A single-page phone UI (plain HTML/CSS/JS, no build step, no new dependency)
- Recovery: speaker reboot, Wi-Fi drop, receiver error, encoder restart → back to playing
- Start at boot (systemd `enable`)

Decided by Martin (2026-11-08, open decisions):

- `stop_after_silence_s` default **180 s**.
- UI language **English**.
- **No authentication** in revision 1 (LAN only). A login using device codes, which Martin
  already has solutions for, is a candidate for a later revision.

Not in revision 1 (do not build): authentication (LAN only, as in the PoC), Home Assistant,
Wi-Fi provisioning, phono stage, enclosure/PCB, custom Cast receiver app, latency reduction
(parked, see README), multi-input, internet access, licence (decided at release).

## Prerequisites carried over from the PoC

These are gates: the milestone that needs them does not start until they pass.

| Item | Why it matters now | Gate for |
| --- | --- | --- |
| Under-voltage fixed (`get_throttled` stays `0x0` for 24 h with UFO202 attached) | Always-on device; under-voltage also causes USB capture loss | R0 |
| Level measurement works on the Pi's ffmpeg 7 (spike, see R0) — **done: inline 100 ms** | Needle-drop detection and live levels depend on it | R1 |
| Noise floor measured (stylus up, silent groove, run-out groove, music) | Sets the detection thresholds | R4 |
| 60-min soak (deferred M5) | Always-on playback must be stable | R5 |
| Wi-Fi power save stays off after reboot | Always-on streaming on −73 dBm | R5 |

## Architecture

```
            phone browser ── GET / , /api/*, /api/events (SSE) ──┐
                                                                 v
  source ─> encoder (ffmpeg: capture + encode + measure) ─> framing ─> hub ─> server (aiohttp)
                │ level / silence lines                                        ^
                v                                                              │ HTTP pull
            detector ──signal on/off──> controller (state machine) ──> cast ──> speaker
                                             │  ^
                         state.json <────────┘  └── CLI / web API commands
```

- **controller.py** (new): the only owner of "what should be playing where". States `IDLE`
  (listening, nothing cast), `STARTING`, `PLAYING`, `STOPPING`, plus a `MANUAL_HOLD` flag. Both the
  CLI and the web API send it commands; the detector sends it signal events. Everything it decides
  is logged to metrics as today.
- **detector.py** (new): turns measurement lines from the encoder's ffmpeg into `signal_on` /
  `signal_off` events with hysteresis, and keeps the latest levels for the UI.
- **state.py** (new): persistent state in `state.json` (last speaker, UI-changed settings).
  `vinylcast.toml` stays the base config; the state file overlays it; the UI never edits the toml.
- **web.py** (new): API routes, SSE, static files, added to the existing aiohttp app on port 8800.
  Stream URLs (`/stream.mp3`, `/stream.flac`) stay unchanged.
- **cast.py** (extended): long-lived discovery browser, volume, detection of "speaker taken over
  by another app", reconnect.
- **service.py** (extended): reconfigure without restarting the process (codec change = encoder
  restart + re-cast).

The encoder runs all the time, also when nothing is cast (needed for detection). With no
listeners the hub just discards frames.

## Auto-cast rules

| Situation | Behaviour |
| --- | --- |
| Signal above threshold for `detect_on_s` (default 1.5 s) while `IDLE` | Cast to the last speaker |
| Signal below threshold for `stop_after_silence_s` (default 180 s) while `PLAYING` | Stop playback on the speaker, back to `IDLE` |
| Side flip, track gaps, quiet passages (< `stop_after_silence_s`) | Keep playing |
| Clicks, thumps, handling noise (shorter than `detect_on_s`) | Ignore |
| User stops via web UI, CLI, Google Home app or voice | `MANUAL_HOLD`: no auto-cast until a full silence period has passed (next record) |
| Last speaker is busy with another app (e.g. Spotify) | Do not take it over; show "speaker busy" in the UI |
| Last speaker not found | Retry discovery for 30 s, then show "speaker unavailable" |
| Receiver goes IDLE/ERROR or connection lost while signal is present | Re-cast, at most 3 times per 10 min, then stop and show the error |
| Auto-cast disabled in settings | Detection still runs (levels in UI), no automatic casting |

## Component specifications

### Measurement in ffmpeg (decided in R0)

Detection needs a level reading several times per second from the capture. The PoC's
`asplit` branch for peak logging broke the stream on ffmpeg 7 (Debian on the Pi) in both
variants tried. R0 tests, on the Pi, in this order:

1. **Inline, pass-through filters on the encoder path** (no split): `silencedetect` for on/off and
   `astats` with `ametadata=print` for levels, on the same chain as the encoder.
2. **Second `-f null` output** fed from an `asplit` without `asetnsamples` (the prime suspect).
3. **Fallback needing a rule change:** ffmpeg writes a low-rate raw PCM side output (e.g. 8 kHz
   mono) on an extra pipe and Python computes RMS/peak. This is measurement, not decoding or
   resampling, but it relaxes the PoC rule "Python never touches audio"; Martin decides.

The first option that keeps the stream intact for 10 minutes with correct levels is used.

**R0 result (Martin's Pi, ffmpeg 7.1.5, 60 s per variant, record playing):**

| Variant | Stream | Readings/s | ffmpeg CPU |
| --- | --- | --- | --- |
| baseline (no measurement) | OK 40/40 kB/s | – | 29.2 % |
| A1 inline astats per frame | OK | 506.8 | 58.3 % |
| **A2 inline asetnsamples 100 ms + astats** | **OK** | **10.0** | **30.7 %** |
| A3 inline silencedetect | OK | – | 28.7 % |
| B1 asplit, per frame, own null output | OK | 506.8 | 66.0 % |
| control: asplit + 10 s blocks (PoC peak logging) | **stalled** ("buffers queued") | 0.1 | 245.7 % |

**Decision: A2** (inline, 100 ms). No Python-side fallback needed. Cost: +1.5 % CPU and up
to ~0.1 s added latency. Speaker check: 3.4 min on HESTON 120 with `measure_levels` on, vinyl,
0 restarts/stalls/drops, loudest peak −9.87 dBFS; accepted by Martin instead of 10 min (the
24 h R5 test covers long runs). `measure_levels` and `peak_log_s = 10` are now the defaults.

**Startup rebuffer (Martin, 2026-11-08):** HESTON 120 always goes BUFFERING ~0.5 s after the
first PLAYING for ~0.5 s (MP3 and FLAC, with and without measurement). The soak criterion
becomes "0 BUFFERING after first PLAYING, with a 5 s startup grace"; rebuffers inside the
grace are reported as `startup_rebuffers`.

### detector.py

- Input: level samples (dBFS, both channels) at ≥ 4 Hz; output: `signal_on` / `signal_off` with
  hysteresis (on above `detect_threshold_db`, off below `threshold − 6 dB`).
- Thresholds from the R0 noise-floor measurement; defaults written into the config.
- Keeps a short history (last 60 s) for the UI meter and diagnostics.

### controller.py

- State machine as above; pure logic with injected clock, detector events and a cast interface
  so it is fully unit-testable without hardware.
- Commands: `play(target)`, `stop()`, `set_target(name)`, `set_volume(x)`, `apply_settings(...)`.
- Every transition is an event in metrics (`ctl_state`, with the reason).

### state.py

- `state.json` next to the config (path configurable), written atomically (temp file + rename).
- Holds: last target name, UI-changed settings (codec, auto-cast on/off, thresholds, silence
  timeout, volume default). Unknown keys ignored, invalid values rejected like config errors.

### web.py and the phone UI

API (JSON, no auth, LAN only):

| Endpoint | Does |
| --- | --- |
| `GET /api/status` | State, target, codec, receiver state, clients, encoder, uptime |
| `GET /api/devices` | Live device + group list (name, kind, model, available) |
| `POST /api/play` `{target?}` / `POST /api/stop` | Manual control (play sets last speaker) |
| `POST /api/volume` `{level}` | Speaker volume 0–1 |
| `GET/POST /api/settings` | Read/change settings (validated, persisted to state.json) |
| `GET /api/diagnostics` | Recent events, Wi-Fi signal, throttling, CPU, last soak summary |
| `GET /api/events` | Server-Sent Events: status changes, levels (4 Hz), notable events |

UI: one page, four sections (Now playing / Speakers / Levels / Settings) plus a Diagnostics view.
Works on a phone browser (Android Chrome and iOS Safari), reconnects SSE automatically,
usable with the Pi's IP or `vinylcast.local` (mDNS; the speaker still gets the IP).

## Configuration (new keys)

| Key | Default | Notes |
| --- | --- | --- |
| `auto_cast` | `true` | Needle-drop casting on/off |
| `detect_threshold_db` | from R0 | dBFS RMS above which "music" is assumed |
| `detect_on_s` | `1.5` | Signal must last this long to start |
| `stop_after_silence_s` | `180` | Silence before stopping (longer than a side flip) |
| `state_path` | `./state.json` | Persistent state |
| `recast_max` | `3` per 10 min | Recovery limit |

## Build order and acceptance criteria

Same working rules as the PoC: one milestone at a time, checks reported with evidence, one
commit per milestone, hardware checks run by Martin with exact commands.

0. **R0 – Prerequisites and spike (Pi).**
   - Power: `get_throttled` = `0x0` after 24 h uptime with the UFO202 attached
   - Measurement spike: one ffmpeg variant (see above) streams to a speaker for 10 min with no
     stall/restart and level lines ≥ 4 Hz; decision recorded
   - Noise floor table: stylus up, silent lead-in, run-out groove, quiet and loud music
     (RMS/peak); thresholds proposed from it
1. **R1 – Control core.** state.py, detector.py (fed by the chosen measurement), controller.py in
   manual mode, CLI on top of the controller, codec change without process restart.
   - Unit tests: state machine (fake clock/cast), detector hysteresis, state file round-trip
   - On the Pi: `vinylcast run` behaves as in the PoC; codec switch while playing re-casts
     within 6 s
2. **R2 – Web API + events.** web.py, continuous discovery, volume.
   - API tests via aiohttp test client; SSE delivers levels ≥ 4 Hz and state changes < 0.5 s
   - On the Pi: `curl` play/stop/volume/settings work against a real speaker
3. **R3 – Phone UI.**
   - On Martin's phone: pick speaker, play/stop, volume, live meter (clip warning above −1 dBFS),
     settings persist across service restart, diagnostics shows Wi-Fi and throttling
   - Page load < 2 s on the Pi over Wi-Fi; no external resources (works without internet)
4. **R4 – Auto-cast.**
   - Simulated (tone/file sources with silence gaps): every rule in the table above has a test
   - On the Pi, 10 cycles needle drop → lift: music on the last speaker within 6 s of needle drop
     each time; stop 180 ± 10 s after lift; a 60 s side flip does not stop playback; manual stop
     is respected until the next record; 2 h idle with the turntable off gives zero false starts
5. **R5 – Unattended acceptance.**
   - 24 h on Wi-Fi, started at boot, mixed idle and listening, including the deferred 60 min
     soak criteria during play (0 encoder restarts, 0 client drops, 0 BUFFERING after first
     PLAYING, `get_throttled` 0)
   - Recovery: speaker power-cycled, Wi-Fi access point restarted, Pi rebooted → playing again
     without touching the Pi
   - README updated (operation, settings, troubleshooting); STATUS.md updated

## Testing

Unit tests for controller, detector, state and API run in CI without hardware (as in the PoC:
`tone`/`file` sources, ffmpeg decode checks, fake Cast). Hardware checks are Martin's runs on the
Pi with the commands given per milestone. The UI is checked by hand on a phone.

## Risks and open decisions

| Risk / decision | Why it matters | Resolved by |
| --- | --- | --- |
| Level measurement on ffmpeg 7 | Two variants already broke the stream; detection depends on it | R0 spike; fallback needs Martin's approval (Python-side RMS) |
| False starts (rumble, handling, preamp thump) | An appliance that starts by itself is worse than none | R0 noise floor, `detect_on_s`, R4 2 h idle test |
| Taking over a busy speaker | Would interrupt someone else's music | Rule: never take over; R4 test |
| Unauthenticated settings on the LAN | Anyone on Wi-Fi can change or stop it | Accepted for rev 1 (Martin); device-code login in a later revision |
| Always-on load on a marginal power supply | Under-voltage seen in PoC | R0 gate |
| Weak Wi-Fi (−73 dBm, 2.4 GHz) | Dropouts during long listening | R5 24 h test; fix by AP placement, not code |
| Default `stop_after_silence_s` | Too short stops on side flips, too long keeps the speaker busy | Decided: 180 s |
| UI language | English or Swedish | Decided: English |

## Instructions for Claude Code

1. Read `PLAN.md`, `CLAUDE.md`, `docs/STATUS.md` and this plan before writing code.
2. Build one milestone at a time; stop after each, report commands and output, wait for approval.
3. No new runtime dependencies without asking (aiohttp, pychromecast, psutil are enough).
   No frontend build tooling; the UI is static files served by aiohttp.
4. Check pychromecast and aiohttp APIs in the installed source before using them (volume,
   receiver app status, SSE/streaming responses).
5. Keep audio processing in ffmpeg unless Martin approves the R0 fallback.
6. Anything needing the Pi, the UFO202 or a speaker: write exact commands, ask for the output.
7. Never re-enable a measurement filter on the stream path without a passing Pi run first.
