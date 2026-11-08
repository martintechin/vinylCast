"""R0 spike: which level-measurement variant keeps the stream intact on this ffmpeg?

    python scripts/spike_measure.py                  # all variants, 60 s each, UFO202 (alsa)
    python scripts/spike_measure.py --seconds 20 --source tone      # quick check, no hardware
    python scripts/spike_measure.py --noise-floor    # guided noise-floor measurement (R0)

Each variant runs ffmpeg exactly like the encoder (source -> MP3 320k on stdout) plus one way
of measuring levels, and reports: did audio flow steadily, did ffmpeg crash or stall, how many
level readings per second arrived, ffmpeg CPU. The VinylCast service must be stopped first
(the hw: device is exclusive). Needs no speaker.
"""

from __future__ import annotations

import argparse
import math
import re
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

import psutil

from vinylcast.config import Config, load_config
from vinylcast.encoder import codec_args
from vinylcast.levels import CLIP_WARN_DBFS
from vinylcast.source import input_args, output_args

ASTATS = "astats=metadata=1:reset=1:measure_perchannel=Peak_level+RMS_level:measure_overall=none"
PRINT = "ametadata=mode=print"
LEVEL_LINE = re.compile(r"lavfi\.astats\.(\d+)\.(Peak_level|RMS_level)=(\S+)")
FRAME_LINE = re.compile(r"\bframe:\d+\s+pts:\d+\s+pts_time:")
SILENCE_LINE = re.compile(r"silence_(start|end)")
BAD = re.compile(r"Assertion|buffers queued|Error|error", re.IGNORECASE)


def variants(cfg: Config) -> dict[str, tuple[str, list[str], list[str]]]:
    """name -> (description, args before the stream output's codec args, args after pipe:1)."""
    block = max(1, cfg.sample_rate // 10)  # 100 ms
    return {
        "0-baseline": ("no measurement (reference)", [], []),
        "A1-inline-per-frame": (
            "inline astats per capture frame on the encoder path",
            ["-af", f"{ASTATS},{PRINT}"], [],
        ),
        "A2-inline-100ms": (
            "inline asetnsamples 100 ms + astats on the encoder path",
            ["-af", f"asetnsamples=n={block}:p=0,{ASTATS},{PRINT}"], [],
        ),
        "A3-inline-silencedetect": (
            "inline silencedetect (on/off events only, no levels)",
            ["-af", "silencedetect=n=-50dB:d=1"], [],
        ),
        "B1-split-null-per-frame": (
            "asplit, measuring branch (astats per frame) to its own -f null output",
            ["-filter_complex", f"[0:a]asplit=2[enc][m];[m]{ASTATS},{PRINT}[meas]",
             "-map", "[enc]"],
            ["-map", "[meas]", "-f", "null", "-"],
        ),
        "C-control-known-bad": (
            "CONTROL: the d9bfcd6 graph (asplit + 10 s asetnsamples -> -f null); failed on the Pi",
            ["-filter_complex",
             f"[0:a]asplit=2[enc][m];[m]asetnsamples=n={cfg.sample_rate * 10}:p=0,"
             f"{ASTATS},{PRINT}[meas]", "-map", "[enc]"],
            ["-map", "[meas]", "-f", "null", "-"],
        ),
    }  # fmt: skip


@dataclass
class Result:
    name: str
    description: str
    returncode: int | None = None
    bytes_per_s: list[int] = field(default_factory=list)
    level_lines: int = 0
    level_frames: int = 0
    silence_lines: int = 0
    first_level_s: float | None = None
    bad_lines: list[str] = field(default_factory=list)
    cpu: list[float] = field(default_factory=list)

    def verdict(self, seconds: float) -> str:
        steady = self.bytes_per_s[2:-1] or self.bytes_per_s
        if not steady or min(steady) < 20_000:
            return "FAIL (stream stalled)"
        if self.returncode not in (None, 0, -15, 255) or any(
            "Assertion" in b for b in self.bad_lines
        ):
            return "FAIL (ffmpeg crashed)"
        return "OK"


def run_variant(cfg: Config, name: str, desc: str, pre: list[str], post: list[str],
                seconds: float) -> Result:  # fmt: skip
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "level+info",
        "-t", f"{seconds:g}", *input_args(cfg), *pre, *output_args(cfg), *codec_args(cfg),
        "pipe:1", *post,
    ]  # fmt: skip
    res = Result(name, desc)
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None and proc.stderr is not None

    def read_err() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if FRAME_LINE.search(line):
                res.level_frames += 1
            elif LEVEL_LINE.search(line):
                res.level_lines += 1
                if res.first_level_s is None:
                    res.first_level_s = round(time.monotonic() - t0, 2)
            elif SILENCE_LINE.search(line):
                res.silence_lines += 1
            elif BAD.search(line) and len(res.bad_lines) < 5:
                res.bad_lines.append(line[:160])

    err = threading.Thread(target=read_err, daemon=True)
    err.start()
    ps = psutil.Process(proc.pid)
    ps.cpu_percent(None)
    bucket_start, bucket = t0, 0
    while True:
        chunk = proc.stdout.read1(65536)
        now = time.monotonic()
        while now - bucket_start >= 1.0:
            res.bytes_per_s.append(bucket)
            bucket, bucket_start = 0, bucket_start + 1.0
            try:
                res.cpu.append(ps.cpu_percent(None))
            except psutil.Error:
                pass
        if not chunk:
            break
        bucket += len(chunk)
        if now - t0 > seconds + 15:  # hung: kill
            proc.kill()
            res.bad_lines.append("killed: no exit 15 s after -t")
            break
    res.returncode = proc.wait()
    err.join(timeout=2)
    return res


def report(results: list[Result], seconds: float) -> None:
    ver = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout
    print(f"\n{ver.splitlines()[0]}\n")
    print(f"{'variant':26s} {'verdict':22s} {'kB/s min/med':>13s} {'levels/s':>8s} "
          f"{'1st lvl':>7s} {'cpu %':>6s}")  # fmt: skip
    for r in results:
        steady = r.bytes_per_s[2:-1] or r.bytes_per_s or [0]
        rate = f"{min(steady) / 1000:4.0f}/{statistics.median(steady) / 1000:4.0f}"
        lps = (
            f"{r.level_frames / seconds:6.1f}"
            if r.level_lines
            else (f"sil:{r.silence_lines}" if r.silence_lines else "     -")
        )
        cpu = f"{statistics.fmean(r.cpu):5.1f}" if r.cpu else "    -"
        first = "-" if r.first_level_s is None else r.first_level_s
        print(f"{r.name:26s} {r.verdict(seconds):22s} {rate:>13s} {lps:>8s} "
              f"{first:>7} {cpu:>6s}")  # fmt: skip
        for b in r.bad_lines:
            print(f"    ! {b}")
    print("\nSend this whole table back. 'levels/s' counts complete readings (both channels).")


def noise_floor(cfg: Config, seconds: float) -> None:
    from vinylcast.levels import build_command, parse

    steps = [
        "Stylus UP, turntable stopped, preamp on",
        "Stylus UP, platter spinning",
        "Stylus in the silent lead-in groove (before track 1)",
        "Stylus in the run-out groove (after the last track)",
        "A QUIET passage of music",
        "A LOUD passage of music",
    ]
    rows = []
    for step in steps:
        input(f"\n{step}. Press Enter to measure {seconds:g} s (Ctrl-C to abort) ")
        cmd = build_command(cfg)
        cmd[cmd.index("-i") : cmd.index("-i")] = ["-t", f"{seconds:g}"]
        out = subprocess.run(cmd, capture_output=True, text=True).stderr.splitlines()
        readings = parse(out)
        rms = [max(r.rms.values()) for r in readings if r.rms]
        peak = [r.max_peak for r in readings]
        finite = [v for v in rms if v != -math.inf]
        rows.append((step, min(finite, default=-math.inf), max(finite, default=-math.inf),
                     max(peak, default=-math.inf)))  # fmt: skip
        print(f"  RMS {rows[-1][1]:.1f} .. {rows[-1][2]:.1f} dBFS, peak {rows[-1][3]:.1f} dBFS")
    print(f"\n{'condition':56s} {'RMS min':>8s} {'RMS max':>8s} {'peak':>7s}")
    for step, lo, hi, pk in rows:
        print(f"{step:56s} {lo:8.1f} {hi:8.1f} {pk:7.1f}")
    noise = max(r[2] for r in rows[:4])
    quiet = rows[4][1]
    print(
        f"\nLoudest non-music RMS: {noise:.1f} dBFS; quietest music RMS: {quiet:.1f} dBFS; "
        f"gap {quiet - noise:.1f} dB. Clip warning above {CLIP_WARN_DBFS:g} dBFS peak."
    )
    print("Send this table back; the detection threshold is chosen from it.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--source", default="alsa", choices=["alsa", "tone"])
    ap.add_argument("--only", help="comma-separated variant names")
    ap.add_argument("--noise-floor", action="store_true")
    args = ap.parse_args()
    cfg = load_config(overrides={"source": args.source})
    if args.noise_floor:
        noise_floor(cfg, min(args.seconds, 10.0))
        return 0
    if args.source == "alsa":
        probe = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-t", "0.5", *input_args(cfg),
             "-f", "null", "-"],
            capture_output=True, text=True,
        )  # fmt: skip
        if probe.returncode != 0:
            print("Cannot open the input, nothing measured:")
            print("  " + (probe.stderr.strip().splitlines() or ["?"])[0])
            print("Is the service still running? sudo systemctl stop vinylcast; "
                  "systemctl is-active vinylcast must say 'inactive'.")  # fmt: skip
            return 2
    chosen = variants(cfg)
    if args.only:
        chosen = {k: v for k, v in chosen.items() if k in args.only.split(",")}
    results = []
    for name, (desc, pre, post) in chosen.items():
        print(f"running {name} ({desc}) for {args.seconds:g} s ...", flush=True)
        results.append(run_variant(cfg, name, desc, pre, post, args.seconds))
    report(results, args.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
