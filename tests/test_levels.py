import math
import subprocess
import sys
import time

import pytest
from conftest import requires_ffmpeg

from vinylcast.config import load_config
from vinylcast.levels import build_command, format_reading, parse

P = "[Parsed_ametadata_3 @ 0x564cf41e1600] "
SAMPLE = [
    P + line
    for line in """frame:0    pts:0       pts_time:0
lavfi.astats.1.Peak_level=-18.063656
lavfi.astats.1.RMS_level=-21.073724
lavfi.astats.2.Peak_level=-0.5
lavfi.astats.2.RMS_level=-9.2
frame:1    pts:44100   pts_time:1
lavfi.astats.1.Peak_level=-inf
lavfi.astats.1.RMS_level=-inf
lavfi.astats.2.Peak_level=-inf
lavfi.astats.2.RMS_level=-inf""".splitlines()
]


def test_parse_and_flag() -> None:
    a, b = parse(SAMPLE)
    assert a.peak == {1: -18.063656, 2: -0.5} and a.rms[2] == -9.2
    assert a.clipping_risk and "lower the preamp" in format_reading(a)
    assert b.max_peak == -math.inf and not b.clipping_risk
    assert "-inf" in format_reading(b)


def test_command_uses_source_and_one_second_blocks() -> None:
    cfg = load_config(overrides={"sample_rate": 48000})
    cmd = build_command(cfg)
    assert cmd[cmd.index("-i") + 1] == "hw:CARD=CODEC,DEV=0"
    af = cmd[cmd.index("-af") + 1]
    assert (
        "asetnsamples=n=48000" in af and "astats=" in af and "aformat=channel_layouts=stereo" in af
    )


@requires_ffmpeg
def test_levels_cli_with_tone() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "vinylcast", "levels", "--source", "tone", "--duration", "3"],
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr
    rows = [line for line in out.stdout.splitlines() if "peak L/R" in line]
    assert len(rows) == 3
    # 440 Hz sine at 1/8 amplitude (-18.1 dBFS) is mono; ffmpeg's mono->stereo upmix puts it
    # at -3 dB in each channel. Real ALSA input is already stereo.
    assert "-21.1  -21.1" in rows[0]
    assert "Loudest peak: -21.1 dBFS; seconds above -1 dBFS: 0" in out.stdout


@requires_ffmpeg
def test_levels_are_live() -> None:
    """Regression (Martin's Pi): readings were buffered by ffmpeg and all arrived at exit.
    The first reading must arrive about 1 s after start, long before the run ends."""
    t0 = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "vinylcast", "levels", "--source", "tone", "--duration", "8"],
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    assert proc.stdout is not None
    first_reading = None
    for line in proc.stdout:
        if "peak L/R" in line:
            first_reading = time.monotonic() - t0
            break
    proc.kill()
    proc.wait()
    assert first_reading is not None and first_reading < 3.0


async def test_no_data_times_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A source that produces nothing must end with an error, not hang."""
    import vinylcast.levels as levels

    monkeypatch.setattr(levels, "NO_DATA_TIMEOUT_S", 0.5)
    monkeypatch.setattr(levels, "build_command", lambda cfg: ["sleep", "30"])
    assert await levels.run_levels(load_config(overrides={"source": "tone"})) == 1
    assert "no data from the source" in capsys.readouterr().out


@requires_ffmpeg
def test_levels_bad_device_fails_cleanly() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "vinylcast", "levels", "--source", "alsa",
         "--alsa-device", "hw:CARD=NOPE,DEV=0", "--duration", "2"],
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    assert out.returncode == 1 and "no readings" in out.stdout
