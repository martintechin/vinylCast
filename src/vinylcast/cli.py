"""Command-line front end. Subcommands: devices, levels, serve, cast, stop, run."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from vinylcast import __version__
from vinylcast.config import CODECS, SOURCES, Config, ConfigError, load_config

EXIT_NOT_IMPLEMENTED = 3  # distinct from argparse usage errors (2)


def _common_parser() -> argparse.ArgumentParser:
    """Options shared by every subcommand. Defaults are SUPPRESS so only explicit flags
    end up in the namespace and override the TOML file."""
    p = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    p.add_argument("--config", type=Path, help="TOML config file")
    g = p.add_argument_group("config overrides")
    g.add_argument("--source", choices=SOURCES)
    g.add_argument("--alsa-device")
    g.add_argument("--source-file")
    g.add_argument("--tone-beep-s", type=float, help="tone source: beep every N s (0 = continuous)")
    g.add_argument("--sample-rate", type=int)
    g.add_argument("--codec", choices=CODECS)
    g.add_argument("--mp3-bitrate")
    g.add_argument("--http-port", type=int)
    g.add_argument("--advertise-ip", help='IP for the stream URL, or "auto"')
    g.add_argument("--client-max-buffer-s", type=float)
    g.add_argument("--metrics-path")
    return p


CONFIG_KEYS = (
    "source",
    "alsa_device",
    "source_file",
    "tone_beep_s",
    "sample_rate",
    "codec",
    "mp3_bitrate",
    "http_port",
    "advertise_ip",
    "client_max_buffer_s",
    "metrics_path",
)


def _not_implemented(milestone: str) -> Callable[[Config, argparse.Namespace], int]:
    def handler(cfg: Config, args: argparse.Namespace) -> int:
        print(f"{args.command}: not implemented until {milestone}", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED

    return handler


def _serve(cfg: Config, args: argparse.Namespace) -> int:
    from vinylcast.service import run_serve

    return asyncio.run(run_serve(cfg))


IMPLEMENTED: dict[str, Callable[[Config, argparse.Namespace], int]] = {"serve": _serve}

COMMANDS: dict[str, tuple[str, str, bool]] = {
    # name: (help, milestone, takes --to)
    "devices": ("List Cast devices and groups", "M3", False),
    "levels": ("Print input peak/RMS in dBFS once per second", "M4", False),
    "serve": ("Encoder + hub + HTTP server, no Cast", "M1", False),
    "cast": ("Send the stream URL of a running server to a target", "M3", True),
    "stop": ("Stop playback on a target", "M3", True),
    "run": ("serve and cast in one process (soak-test entry point)", "M3", True),
}


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(prog="vinylcast", description="VinylCast PoC")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, (help_text, milestone, takes_target) in COMMANDS.items():
        sp = sub.add_parser(name, parents=[common], help=help_text, description=help_text)
        if takes_target:
            sp.add_argument(
                "--to",
                dest="cast_target",
                metavar="NAME",
                default=argparse.SUPPRESS,
                help="Friendly name of a Cast device or group",
            )
        sp.set_defaults(handler=IMPLEMENTED.get(name) or _not_implemented(milestone))
    return parser


def config_from_args(args: argparse.Namespace) -> Config:
    ns = vars(args)
    overrides = {k: ns[k] for k in (*CONFIG_KEYS, "cast_target") if k in ns}
    return load_config(ns.get("config"), overrides)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = config_from_args(args)
    except ConfigError as e:
        parser.error(str(e))
    return args.handler(cfg, args)
