"""Configuration: defaults < TOML file < UI settings (state.json) < CLI overrides."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Any

SOURCES = ("alsa", "tone", "file")
CODECS = ("mp3", "flac")


class ConfigError(ValueError):
    """Raised for unknown keys, wrong types or invalid values."""


@dataclass(frozen=True)
class Config:
    source: str = "alsa"
    # hw: (not plughw:) so an unsupported rate fails instead of being resampled by ALSA.
    # UFO202 native capture: S16_LE stereo at 48000/44100/32000/22050/16000/11025 Hz.
    alsa_device: str = "hw:CARD=CODEC,DEV=0"
    source_file: str | None = None
    tone_beep_s: float = 0.0  # 0 = continuous tone; N > 0 = 0.1 s beep every N s (latency checks)
    sample_rate: int = 44100
    codec: str = "mp3"
    mp3_bitrate: str = "320k"
    http_port: int = 8800
    advertise_ip: str | None = None  # None = auto-detect from the default route
    cast_target: str | None = None
    client_max_buffer_s: float = 10.0
    # Level measurement inline in the encoder's ffmpeg (100 ms blocks, R0 variant A2: passed
    # the spike and a 3.4 min speaker run on the Pi's ffmpeg 7, +1.5 % CPU). Default on (R0).
    measure_levels: bool = True
    # Log the input peak every N s (needs measure_levels); 0 = off.
    peak_log_s: float = 10.0
    metrics_path: str = "./metrics.jsonl"  # strftime codes allowed, e.g. soak-%Y%m%d-%H%M.jsonl
    # Revision 1: auto-cast on needle drop (docs/PLAN-rev1.md)
    auto_cast: bool = True
    # RMS dBFS (R0 noise floor, Martin's Pi): music -34..-25, groove noise up to -46/-50,
    # stylus up -64..-55. On at -40, off below -46 (6 dB hysteresis).
    detect_threshold_db: float = -40.0
    detect_on_s: float = 1.5  # signal must last this long before casting
    stop_after_silence_s: float = 180.0  # silence before stopping (longer than a side flip)
    recast_max: int = 3  # automatic re-casts allowed per 10 minutes
    state_path: str = "./state.json"

    @property
    def mp3_bitrate_bps(self) -> int:
        return int(self.mp3_bitrate[:-1]) * 1000


_TYPES: dict[str, tuple[type, ...]] = {
    "source": (str,),
    "alsa_device": (str,),
    "source_file": (str,),
    "tone_beep_s": (int, float),
    "sample_rate": (int,),
    "codec": (str,),
    "mp3_bitrate": (str,),
    "http_port": (int,),
    "advertise_ip": (str,),
    "cast_target": (str,),
    "client_max_buffer_s": (int, float),
    "measure_levels": (bool,),
    "peak_log_s": (int, float),
    "metrics_path": (str,),
    "auto_cast": (bool,),
    "detect_threshold_db": (int, float),
    "detect_on_s": (int, float),
    "stop_after_silence_s": (int, float),
    "recast_max": (int,),
    "state_path": (str,),
}
_FLOATS = (
    "client_max_buffer_s", "tone_beep_s", "peak_log_s",
    "detect_threshold_db", "detect_on_s", "stop_after_silence_s",
)  # fmt: skip

# Keys the web UI may change (persisted in state.json). Never paths, devices or ports.
UI_SETTABLE = frozenset(
    {"codec", "mp3_bitrate", "auto_cast", "detect_threshold_db", "detect_on_s",
     "stop_after_silence_s"}
)  # fmt: skip
assert set(_TYPES) == {f.name for f in fields(Config)}


def _coerce(values: dict[str, Any], origin: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key not in _TYPES:
            raise ConfigError(f"{origin}: unknown key {key!r}")
        if value is None:
            out[key] = None
            continue
        # bool is a subclass of int: only accept it where a bool is expected.
        wants_bool = _TYPES[key] == (bool,)
        if isinstance(value, bool) != wants_bool or not isinstance(value, _TYPES[key]):
            raise ConfigError(f"{origin}: {key} has wrong type {type(value).__name__}")
        if key == "advertise_ip" and value == "auto":
            value = None
        if key in _FLOATS:
            value = float(value)
        out[key] = value
    return out


def _validate(cfg: Config) -> None:
    if cfg.source not in SOURCES:
        raise ConfigError(f"source must be one of {SOURCES}, got {cfg.source!r}")
    if cfg.codec not in CODECS:
        raise ConfigError(f"codec must be one of {CODECS}, got {cfg.codec!r}")
    if cfg.source == "file" and not cfg.source_file:
        raise ConfigError("source 'file' requires source_file")
    if not 1 <= cfg.http_port <= 65535:
        raise ConfigError(f"http_port out of range: {cfg.http_port}")
    if cfg.sample_rate <= 0:
        raise ConfigError(f"sample_rate must be positive: {cfg.sample_rate}")
    if not re.fullmatch(r"[1-9][0-9]*k", cfg.mp3_bitrate):
        raise ConfigError(f"mp3_bitrate must look like '320k', got {cfg.mp3_bitrate!r}")
    if cfg.tone_beep_s < 0:
        raise ConfigError(f"tone_beep_s must be >= 0: {cfg.tone_beep_s}")
    if cfg.peak_log_s < 0:
        raise ConfigError(f"peak_log_s must be >= 0: {cfg.peak_log_s}")
    if cfg.client_max_buffer_s <= 0:
        raise ConfigError(f"client_max_buffer_s must be positive: {cfg.client_max_buffer_s}")
    if not -120 <= cfg.detect_threshold_db <= 0:
        raise ConfigError(f"detect_threshold_db must be in -120..0: {cfg.detect_threshold_db}")
    if cfg.detect_on_s <= 0:
        raise ConfigError(f"detect_on_s must be positive: {cfg.detect_on_s}")
    if cfg.stop_after_silence_s <= 0:
        raise ConfigError(f"stop_after_silence_s must be positive: {cfg.stop_after_silence_s}")
    if cfg.recast_max < 0:
        raise ConfigError(f"recast_max must be >= 0: {cfg.recast_max}")


def validate_ui_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Check settings coming from the web UI / state.json: only UI_SETTABLE keys, right types."""
    bad = set(settings) - UI_SETTABLE
    if bad:
        raise ConfigError(f"settings: not changeable from the UI: {', '.join(sorted(bad))}")
    return _coerce(settings, "settings")


def load_config(
    path: Path | None = None,
    overrides: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> Config:
    """Build a Config: defaults < TOML (`path`) < UI `settings` < CLI `overrides`.
    `overrides` holds only values the user set explicitly."""
    cfg = Config()
    if path is not None:
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"{path}: {e}") from e
        cfg = replace(cfg, **_coerce(data, str(path)))
    if settings:
        cfg = replace(cfg, **validate_ui_settings(settings))
    if overrides:
        cfg = replace(cfg, **_coerce(overrides, "command line"))
    _validate(cfg)
    return cfg
