"""Wires source -> encoder -> hub -> server and the metrics sampler into one service.

Kept separate from cli.py so revision 1 can drive the same service from a web UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from typing import Any

from vinylcast.config import Config
from vinylcast.detector import Level
from vinylcast.encoder import Encoder, build_command
from vinylcast.framing import make_framer
from vinylcast.hub import Hub
from vinylcast.levels import LevelReader, PeakLogger
from vinylcast.metrics import Metrics, PiHealth, Sampler
from vinylcast.server import StreamServer, stream_url


class Service:
    def __init__(self, cfg: Config, *, sample_interval_s: float = 5.0, **encoder_kw: Any) -> None:
        self.cfg = cfg
        self.metrics = Metrics(cfg.metrics_path)
        self.hub = Hub(cfg.client_max_buffer_s, self.metrics)
        self.framer = make_framer(cfg.codec, self._on_flac_header)
        self.level_reader = LevelReader()
        self.peaks = PeakLogger(self.metrics, cfg.peak_log_s)
        self.latest_level: Level | None = None
        self.encoder = Encoder(
            build_command(cfg),
            self._on_chunk,
            self.metrics,
            on_start=self._on_encoder_start,
            on_info=self._on_ffmpeg_info,
            **encoder_kw,
        )
        self.server = StreamServer(
            cfg, self.hub, self.metrics, self.encoder.status, lambda: self.framer.stream_header
        )
        self.sampler = Sampler(
            self.metrics, self.hub.stats, lambda: self.encoder.pid, sample_interval_s
        )
        self.health = PiHealth(self.metrics)
        self._tasks: list[asyncio.Task[None]] = []

    def _on_chunk(self, data: bytes) -> None:
        skipped = self.framer.skipped_bytes
        for frame in self.framer.feed(data):
            self.hub.publish(frame.data, frame.duration_s)
        if self.framer.skipped_bytes != skipped:
            self.metrics.event(
                "framing_skipped", bytes=self.framer.skipped_bytes - skipped, codec=self.cfg.codec
            )

    def _on_encoder_start(self) -> None:
        self.framer.reset()
        self.level_reader.reset()
        self.peaks.flush()

    def _on_ffmpeg_info(self, line: str) -> None:
        level = self.level_reader.feed(line)
        if level is not None:
            self.latest_level = level
            self.peaks.add(level)

    def _on_flac_header(self, header: bytes, old: bytes | None) -> None:
        if old is None:
            self.metrics.event("flac_header", status="captured", bytes=len(header))
        elif header == old:
            self.metrics.event("flac_header", status="unchanged", bytes=len(header))
        else:
            # Clients already decoding with the old STREAMINFO cannot continue safely.
            self.metrics.event("flac_header", status="changed", bytes=len(header))
            self.hub.close("flac_header_changed")

    async def start(self, port: int | None = None) -> int:
        bound = await self.server.start(port=port)
        self._tasks = [
            asyncio.create_task(self.encoder.run(), name="encoder"),
            asyncio.create_task(self.sampler.run(), name="sampler"),
            asyncio.create_task(self.health.run(), name="pi-health"),
        ]
        return bound

    async def stop(self) -> None:
        await self.encoder.stop()
        self.hub.close("shutdown")
        await self.server.stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.metrics.event("service_stop")
        self.metrics.close()


async def _start_or_report(service: Service) -> bool:
    """Start the service; on a bind failure (port in use) print one clear line instead of
    a traceback, log it, clean up and return False."""
    try:
        await service.start()
    except OSError as e:
        port = service.cfg.http_port
        service.metrics.event("server_bind_failed", port=port, error=str(e))
        print(
            f"Cannot listen on port {port}: {e.strerror or e}. Another VinylCast running? "
            f"Check with: pgrep -af vinylcast",
            file=sys.stderr,
            flush=True,
        )
        await service.stop()
        return False
    return True


async def run_serve(cfg: Config) -> int:
    service = Service(cfg)
    service.metrics.event("service_start", config=cfg.__dict__)
    if not await _start_or_report(service):
        return 1
    print(f"Streaming at {stream_url(cfg)}  (Ctrl-C to stop)", flush=True)
    try:
        await _wait_for_signal()
    finally:
        await service.stop()
    return 0


async def _wait_for_signal() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()


async def run_cast(cfg: Config) -> int:
    """`run`: serve + cast in one process. Keeps logging Cast state until SIGINT/SIGTERM,
    then stops playback on the target and shuts down. Soak-test entry point."""
    from vinylcast.cast import CastError, CastSession
    from vinylcast.server import CONTENT_TYPES

    assert cfg.cast_target is not None
    service = Service(cfg)
    service.metrics.event("service_start", config=cfg.__dict__, mode="run")
    if not await _start_or_report(service):
        return 1
    url = stream_url(cfg)
    print(f"Streaming at {url}", flush=True)
    session = CastSession(cfg.cast_target, service.metrics)
    try:
        try:
            await session.connect()
        except CastError as e:
            print(f"run: {e}", flush=True)
            return 1
        latency = await session.play(url, CONTENT_TYPES[cfg.codec])
        if latency is None:
            print(f"Sent to {cfg.cast_target!r}, but no PLAYING state yet; still logging "
                  "(Ctrl-C to stop)", flush=True)  # fmt: skip
        else:
            print(f"Playing on {cfg.cast_target!r} after {latency:.2f} s  (Ctrl-C to stop)",
                  flush=True)  # fmt: skip
        await _wait_for_signal()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.stop(), 15)
        return 0
    finally:
        await session.disconnect()
        await service.stop()
