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


def test_peak_measurement_is_a_separate_null_output() -> None:
    """Regression (Martin's Pi, ffmpeg 7): an in-graph anullsink made ffmpeg abort with
    'Assertion best_input >= 0 failed' and no audio out. The measuring branch must be its
    own -f null output; the stream output must come first and keep its codec options."""
    cmd = build_command(load_config(overrides={"source": "tone", "peak_log_s": 10}))
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "anullsink" not in graph and graph.endswith("[peak]")
    assert "asetnsamples=n=441000" in graph
    stream_out = cmd.index("pipe:1")
    assert cmd[cmd.index("-map") + 1] == "[enc]" and cmd.index("-c:a") < stream_out
    assert cmd[stream_out + 1 :] == ["-map", "[peak]", "-f", "null", "-"]


def test_peak_logging_off_has_no_graph() -> None:
    cmd = build_command(load_config(overrides={"source": "tone", "peak_log_s": 0}))
    assert "-filter_complex" not in cmd and cmd[-1] == "pipe:1"
