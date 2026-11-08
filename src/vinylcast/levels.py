"""`levels`: live input level meter. ffmpeg's astats measures peak and RMS per channel
over 1-second blocks; Python only parses the printed numbers.

Loudest passages must stay below CLIP_WARN_DBFS: the UFO202 has no input gain control,
so clipping can only be fixed by lowering the preamp's output level.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import signal
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from vinylcast import source
from vinylcast.config import Config
from vinylcast.metrics import Metrics

CLIP_WARN_DBFS = -1.0
# Readings come from ametadata's log output on stderr ("[Parsed_ametadata_3 @ 0x..] frame:..").
# Not file=- (stdout): ffmpeg buffers that and flushed everything only at exit (bug found on
# the Pi: `levels` looked hung). stderr is written line by line.
_FRAME = re.compile(r"\bframe:\d+\s+pts:\d+\s+pts_time:([\d.]+)")
_STAT = re.compile(r"\blavfi\.astats\.(\d+)\.(Peak_level|RMS_level)=(\S+)")
_ERROR = re.compile(r"error|cannot|failed|no such", re.IGNORECASE)
NO_DATA_TIMEOUT_S = 5.0


def build_command(cfg: Config, ffmpeg: str = "ffmpeg") -> list[str]:
    chain = ",".join(
        [
            "aformat=channel_layouts=stereo",
            f"asetnsamples=n={cfg.sample_rate}:p=0",  # 1-second blocks
            "astats=metadata=1:reset=1:measure_perchannel=Peak_level+RMS_level"
            ":measure_overall=none",
            "ametadata=mode=print",
        ]
    )
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
        *source.input_args(cfg), "-af", chain, "-f", "null", "-",
    ]  # fmt: skip


def _db(value: str) -> float:
    try:
        v = float(value)
    except ValueError:
        return -math.inf  # astats prints "-inf" for digital silence
    return v


@dataclass
class Reading:
    t: float
    peak: dict[int, float] = field(default_factory=dict)
    rms: dict[int, float] = field(default_factory=dict)

    @property
    def max_peak(self) -> float:
        return max(self.peak.values(), default=-math.inf)

    @property
    def clipping_risk(self) -> bool:
        return self.max_peak > CLIP_WARN_DBFS


def parse(lines: list[str]) -> list[Reading]:
    """Parse ametadata print output into one Reading per block."""
    out: list[Reading] = []
    for line in lines:
        if m := _FRAME.search(line):
            out.append(Reading(float(m.group(1))))
        elif (m := _STAT.search(line)) and out:
            ch, key, value = int(m.group(1)), m.group(2), _db(m.group(3))
            (out[-1].peak if key == "Peak_level" else out[-1].rms)[ch] = value
    return out


def format_reading(r: Reading) -> str:
    def f(v: float) -> str:
        return "  -inf" if v == -math.inf else f"{v:6.1f}"

    chans = sorted(r.peak)
    peaks = " ".join(f(r.peak[c]) for c in chans)
    rms = " ".join(f(r.rms.get(c, -math.inf)) for c in chans)
    flag = (
        f"  <-- above {CLIP_WARN_DBFS:g} dBFS: lower the preamp output" if r.clipping_risk else ""
    )
    return f"{r.t:6.0f}s  peak L/R {peaks}  rms L/R {rms} dBFS{flag}"


async def readings(cfg: Config, duration_s: float | None = None) -> AsyncIterator[Reading]:
    cmd = build_command(cfg)
    if duration_s is not None:
        # As an input option, so the source itself stops (output -t overshoots by blocks).
        i = cmd.index("-i")
        cmd[i:i] = ["-t", f"{duration_s:g}"]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stderr is not None
    pending: list[str] = []
    try:
        while True:
            try:
                raw = await asyncio.wait_for(proc.stderr.readline(), NO_DATA_TIMEOUT_S)
            except TimeoutError:
                raise NoDataError(f"no data from the source for {NO_DATA_TIMEOUT_S:g} s") from None
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if _FRAME.search(line):
                if pending:
                    yield from_block(pending)
                pending = [line]
            elif _STAT.search(line):
                pending.append(line)
                if len(pending) == 5:  # frame line + peak/RMS for both channels: complete
                    yield from_block(pending)
                    pending = []
            elif _ERROR.search(line):
                print(f"ffmpeg: {line}", file=sys.stderr, flush=True)
        if pending:
            yield from_block(pending)
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        await proc.wait()


class NoDataError(RuntimeError):
    pass


def from_block(lines: list[str]) -> Reading:
    (reading,) = parse(lines)
    return reading


async def run_levels(cfg: Config, duration_s: float | None = None) -> int:
    print(f"Input levels from {cfg.source} (Ctrl-C to stop). Keep peaks below "
          f"{CLIP_WARN_DBFS:g} dBFS.", flush=True)  # fmt: skip
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    loudest = -math.inf
    over = count = 0
    with contextlib.suppress(asyncio.CancelledError):
        try:
            async for r in readings(cfg, duration_s):
                count += 1
                loudest = max(loudest, r.max_peak)
                over += r.clipping_risk
                print(format_reading(r), flush=True)
        except NoDataError as e:
            print(f"levels: {e}", flush=True)
            return 1
    if count == 0:
        print("levels: no readings (see the ffmpeg error above; is the device right?)",
              flush=True)  # fmt: skip
        return 1
    print(f"Loudest peak: {loudest:.1f} dBFS; seconds above {CLIP_WARN_DBFS:g} dBFS: {over}",
          flush=True)  # fmt: skip
    return 0


class PeakLogger:
    """Turns the encoder's info-level peak lines into `input_peak` events, one per block.
    A block is emitted when the next block's frame line arrives (the last block of a
    process is dropped; at 10 s blocks that is irrelevant for a soak summary)."""

    def __init__(self, metrics: Metrics) -> None:
        self._metrics = metrics
        self._pending: list[str] = []

    def feed(self, line: str) -> None:
        if _FRAME.search(line):
            self.flush()
            self._pending = [line]
        elif _STAT.search(line) and self._pending:
            self._pending.append(line)

    def flush(self) -> None:
        if len(self._pending) > 1:
            r = from_block(self._pending)
            self._metrics.event(
                "input_peak",
                t=r.t,
                peak_db=None if r.max_peak == -math.inf else round(r.max_peak, 2),
                channels={
                    str(c): (None if v == -math.inf else round(v, 2)) for c, v in r.peak.items()
                },
                clip_risk=r.clipping_risk,
            )
        self._pending = []
