from vinylcast.config import load_config
from vinylcast.encoder import build_command
from vinylcast.source import input_args, output_args


def test_alsa() -> None:
    cfg = load_config(overrides={"sample_rate": 48000})
    assert input_args(cfg) == [
        "-f", "alsa", "-sample_rate", "48000", "-channels", "2", "-i", "hw:CARD=CODEC,DEV=0",
    ]  # fmt: skip


def test_tone_continuous() -> None:
    args = input_args(load_config(overrides={"source": "tone"}))
    assert args == ["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100"]


def test_tone_beep() -> None:
    args = input_args(load_config(overrides={"source": "tone", "tone_beep_s": 5}))
    assert args[-1].startswith("aevalsrc=") and "mod(t\\,5)" in args[-1]


def test_file() -> None:
    args = input_args(load_config(overrides={"source": "file", "source_file": "a.flac"}))
    assert args == ["-re", "-stream_loop", "-1", "-i", "a.flac"]


def test_output_and_command() -> None:
    cfg = load_config(overrides={"source": "tone"})
    assert output_args(cfg) == ["-ac", "2", "-ar", "44100"]
    cmd = build_command(cfg)
    assert cmd[0] == "ffmpeg" and "pipe:1" in cmd  # stream output (null peak output follows)
    assert cmd[cmd.index("-c:a") + 1] == "libmp3lame"
    assert cmd[cmd.index("-b:a") + 1] == "320k"
    assert cmd[cmd.index("-id3v2_version") + 1] == "0"


def test_level_measurement_is_inline_100ms() -> None:
    """R0 decision (Pi, ffmpeg 7): inline 100 ms blocks on the encoder path. No asplit /
    filter_complex: a split branch with long blocks stalled the stream on ffmpeg 7."""
    cmd = build_command(load_config(overrides={"source": "tone", "measure_levels": True}))
    assert "-filter_complex" not in cmd and cmd[-1] == "pipe:1"
    chain = cmd[cmd.index("-af") + 1]
    assert chain.startswith("asetnsamples=n=4410:p=0,astats=")
    assert chain.endswith("ametadata=mode=print")
    assert cmd.index("-af") < cmd.index("-c:a")


def test_no_measurement_by_default() -> None:
    cmd = build_command(load_config(overrides={"source": "tone"}))
    assert "-af" not in cmd and "-filter_complex" not in cmd and cmd[-1] == "pipe:1"
