"""Builds ffmpeg input arguments for the alsa, tone and file sources.

Everything here is argument construction only; ffmpeg does capture, generation,
up-mixing and resampling.
"""

from __future__ import annotations

from vinylcast.config import Config

TONE_HZ = 440
BEEP_LEN_S = 0.1


def input_args(cfg: Config) -> list[str]:
    """ffmpeg arguments that open the configured source (everything up to and including -i)."""
    rate = cfg.sample_rate
    if cfg.source == "alsa":
        return ["-f", "alsa", "-sample_rate", str(rate), "-channels", "2", "-i", cfg.alsa_device]
    if cfg.source == "tone":
        if cfg.tone_beep_s > 0:
            # Short beep every N seconds; commas inside the expression are escaped for lavfi.
            expr = f"0.25*sin(2*PI*{TONE_HZ}*t)*lt(mod(t\\,{cfg.tone_beep_s:g})\\,{BEEP_LEN_S:g})"
            graph = f"aevalsrc=exprs={expr}:s={rate}"
        else:
            graph = f"sine=frequency={TONE_HZ}:sample_rate={rate}"
        return ["-re", "-f", "lavfi", "-i", graph]
    if cfg.source == "file":
        assert cfg.source_file is not None  # enforced by config validation
        return ["-re", "-stream_loop", "-1", "-i", cfg.source_file]
    raise ValueError(f"unknown source {cfg.source!r}")


def output_args(cfg: Config) -> list[str]:
    """Normalise every source to stereo at the configured rate (done by ffmpeg)."""
    return ["-ac", "2", "-ar", str(cfg.sample_rate)]
