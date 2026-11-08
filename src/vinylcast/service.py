"""Wires source -> encoder -> hub -> server and the metrics sampler into one service.

Kept separate from cli.py so revision 1 can drive the same service from a web UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Callable
from typing import Any

from vinylcast.config import Config
from vinylcast.encoder import Encoder, build_command
from vinylcast.hub import Hub
from vinylcast.metrics import Metrics, Sampler
from vinylcast.server import StreamServer, stream_url


def chunk_duration_fn(cfg: Config) -> Callable[[bytes], float]:
    """Audio duration of an encoded chunk. MP3 is CBR, so bytes map directly to time.
    M2 replaces this with exact per-frame durations from framing."""
    if cfg.codec != "mp3":
        raise NotImplementedError("FLAC streaming needs M2 framing")
    bytes_per_s = cfg.mp3_bitrate_bps / 8
    return lambda data: len(data) / bytes_per_s


class Service:
    def __init__(self, cfg: Config, *, sample_interval_s: float = 5.0, **encoder_kw: Any) -> None:
        self.cfg = cfg
        self.metrics = Metrics(cfg.metrics_path)
        self.hub = Hub(cfg.client_max_buffer_s, self.metrics)
        duration = chunk_duration_fn(cfg)
        self.encoder = Encoder(
            build_command(cfg),
            lambda data: self.hub.publish(data, duration(data)),
            self.metrics,
            **encoder_kw,
        )
        self.server = StreamServer(cfg, self.hub, self.metrics, self.encoder.status)
        self.sampler = Sampler(
            self.metrics, self.hub.stats, lambda: self.encoder.pid, sample_interval_s
        )
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self, port: int | None = None) -> int:
        bound = await self.server.start(port=port)
        self._tasks = [
            asyncio.create_task(self.encoder.run(), name="encoder"),
            asyncio.create_task(self.sampler.run(), name="sampler"),
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


async def run_serve(cfg: Config) -> int:
    service = Service(cfg)
    service.metrics.event("service_start", config=cfg.__dict__)
    await service.start()
    print(f"Streaming at {stream_url(cfg)}  (Ctrl-C to stop)", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await service.stop()
    return 0
