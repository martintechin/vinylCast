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
    g.add_argument("--measure-levels", action=argparse.BooleanOptionalAction,
                   help="measure input levels in the encoder (100 ms blocks)")  # fmt: skip
    g.add_argument("--peak-log-s", type=float, help="log input peak every N s (0 = off)")
    g.add_argument("--log-level", choices=("warning", "info", "debug"),
                   help="console/journal detail; metrics file gets everything")  # fmt: skip
    g.add_argument("--auto-cast", action=argparse.BooleanOptionalAction)
    g.add_argument("--detect-threshold-db", type=float)
    g.add_argument("--detect-on-s", type=float)
    g.add_argument("--stop-after-silence-s", type=float)
    g.add_argument("--recast-max", type=int)
    g.add_argument("--state-path")
    g.add_argument("--metrics-path", help="JSONL log; strftime codes allowed (%%Y%%m%%d-%%H%%M)")
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
    "measure_levels",
    "peak_log_s",
    "metrics_path",
    "log_level",
    "auto_cast",
    "detect_threshold_db",
    "detect_on_s",
    "stop_after_silence_s",
    "recast_max",
    "state_path",
)


def _not_implemented(milestone: str) -> Callable[[Config, argparse.Namespace], int]:
    def handler(cfg: Config, args: argparse.Namespace) -> int:
        print(f"{args.command}: not implemented until {milestone}", file=sys.stderr)
        return EXIT_NOT_IMPLEMENTED

    return handler


def _serve(cfg: Config, args: argparse.Namespace) -> int:
    from vinylcast.service import run_serve

    return asyncio.run(run_serve(cfg))


def _need_target(cfg: Config, command: str) -> bool:
    if cfg.cast_target:
        return True
    print(f"{command}: no target; pass --to NAME or set cast_target", file=sys.stderr)
    return False


def _devices(cfg: Config, args: argparse.Namespace) -> int:
    from vinylcast.cast import discover

    targets = discover()
    if not targets:
        print("No Cast devices or groups found (same subnet? mDNS allowed?)", file=sys.stderr)
        return 1
    rows = [("NAME", "KIND", "CAST TYPE", "MODEL", "HOST:PORT")] + [
        (t.name, t.kind, t.cast_type, t.model, f"{t.host}:{t.port}") for t in targets
    ]
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip())
    return 0


async def _cast_async(cfg: Config) -> int:
    import dataclasses

    import aiohttp

    from vinylcast.cast import CastError, CastSession
    from vinylcast.metrics import Metrics
    from vinylcast.server import CONTENT_TYPES, stream_url

    assert cfg.cast_target is not None
    health_url = f"http://127.0.0.1:{cfg.http_port}/health"
    try:
        async with (
            aiohttp.ClientSession() as s,
            s.get(health_url, timeout=aiohttp.ClientTimeout(total=3)) as r,
        ):
            health = await r.json()
    except (aiohttp.ClientError, TimeoutError) as e:
        print(f"cast: no running server at {health_url} ({e}); start `vinylcast serve` first",
              file=sys.stderr)  # fmt: skip
        return 1
    cfg = dataclasses.replace(cfg, codec=health["codec"])  # the running server decides
    url = stream_url(cfg)
    metrics = Metrics(cfg.metrics_path)
    session = CastSession(cfg.cast_target, metrics)
    try:
        await session.connect()
        latency = await session.play(url, CONTENT_TYPES[cfg.codec])
    except CastError as e:
        print(f"cast: {e}", file=sys.stderr)
        return 1
    finally:
        await session.disconnect()
        metrics.close()
    if latency is None:
        print(f"Sent {url} to {cfg.cast_target!r}, but it did not report PLAYING", file=sys.stderr)
        return 1
    print(f"Playing {url} on {cfg.cast_target!r} after {latency:.2f} s")
    return 0


def _cast(cfg: Config, args: argparse.Namespace) -> int:
    return asyncio.run(_cast_async(cfg)) if _need_target(cfg, "cast") else 2


async def _stop_async(cfg: Config) -> int:
    from vinylcast.cast import CastError, CastSession
    from vinylcast.metrics import Metrics

    assert cfg.cast_target is not None
    metrics = Metrics(cfg.metrics_path)
    session = CastSession(cfg.cast_target, metrics)
    try:
        await session.connect()
        await session.stop()
    except CastError as e:
        print(f"stop: {e}", file=sys.stderr)
        return 1
    finally:
        await session.disconnect()
        metrics.close()
    print(f"Stopped playback on {cfg.cast_target!r}")
    return 0


def _stop(cfg: Config, args: argparse.Namespace) -> int:
    return asyncio.run(_stop_async(cfg)) if _need_target(cfg, "stop") else 2


def _run(cfg: Config, args: argparse.Namespace) -> int:
    from vinylcast.service import run_cast

    return asyncio.run(run_cast(cfg)) if _need_target(cfg, "run") else 2


def _levels(cfg: Config, args: argparse.Namespace) -> int:
    from vinylcast.levels import run_levels

    return asyncio.run(run_levels(cfg, getattr(args, "duration", None)))


IMPLEMENTED: dict[str, Callable[[Config, argparse.Namespace], int]] = {
    "serve": _serve,
    "levels": _levels,
    "devices": _devices,
    "cast": _cast,
    "stop": _stop,
    "run": _run,
    "daemon": lambda cfg, args: _daemon(args),
}

COMMANDS: dict[str, tuple[str, str, bool]] = {
    # name: (help, milestone, takes --to)
    "devices": ("List Cast devices and groups", "M3", False),
    "levels": ("Print input peak/RMS in dBFS once per second", "M4", False),
    "serve": ("Encoder + hub + HTTP server, no Cast", "M1", False),
    "cast": ("Send the stream URL of a running server to a target", "M3", True),
    "stop": ("Stop playback on a target", "M3", True),
    "run": ("serve and cast in one process (soak-test entry point)", "M3", True),
    "daemon": ("always on: cast to the last speaker on needle drop (revision 1)", "R1", True),
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
        if name == "levels":
            sp.add_argument("--duration", type=float, default=argparse.SUPPRESS,
                            help="stop after N seconds (default: until Ctrl-C)")  # fmt: skip
        sp.set_defaults(handler=IMPLEMENTED.get(name) or _not_implemented(milestone))
    return parser


def overrides_from_args(args: argparse.Namespace) -> dict[str, object]:
    ns = vars(args)
    return {k: ns[k] for k in (*CONFIG_KEYS, "cast_target") if k in ns}


def config_from_args(args: argparse.Namespace) -> Config:
    return load_config(vars(args).get("config"), overrides_from_args(args))


def _daemon(args: argparse.Namespace) -> int:
    from vinylcast.service import run_daemon

    return asyncio.run(run_daemon(vars(args).get("config"), overrides_from_args(args)))


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
    logging.getLogger().setLevel(cfg.log_level.upper())
    return args.handler(cfg, args)
