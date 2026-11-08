"""Configuration: defaults < TOML file < CLI overrides."""

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
    alsa_device: str = "plughw:CARD=CODEC,DEV=0"
    source_file: str | None = None
    tone_beep_s: float = 0.0  # 0 = continuous tone; N > 0 = 0.1 s beep every N s (latency checks)
    sample_rate: int = 44100
    codec: str = "mp3"
    mp3_bitrate: str = "320k"
    http_port: int = 8800
    advertise_ip: str | None = None  # None = auto-detect from the default route
    cast_target: str | None = None
    client_max_buffer_s: float = 10.0
    metrics_path: str = "./metrics.jsonl"

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
    "metrics_path": (str,),
}
assert set(_TYPES) == {f.name for f in fields(Config)}


def _coerce(values: dict[str, Any], origin: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key not in _TYPES:
            raise ConfigError(f"{origin}: unknown key {key!r}")
        if value is None:
            out[key] = None
            continue
        # bool is a subclass of int; reject it explicitly.
        if isinstance(value, bool) or not isinstance(value, _TYPES[key]):
            raise ConfigError(f"{origin}: {key} has wrong type {type(value).__name__}")
        if key == "advertise_ip" and value == "auto":
            value = None
        if key in ("client_max_buffer_s", "tone_beep_s"):
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
    if cfg.client_max_buffer_s <= 0:
        raise ConfigError(f"client_max_buffer_s must be positive: {cfg.client_max_buffer_s}")


def load_config(path: Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    """Build a Config. `overrides` holds only values the user set explicitly."""
    cfg = Config()
    if path is not None:
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"{path}: {e}") from e
        cfg = replace(cfg, **_coerce(data, str(path)))
    if overrides:
        cfg = replace(cfg, **_coerce(overrides, "command line"))
    _validate(cfg)
    return cfg
