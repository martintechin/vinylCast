"""Wires source -> encoder -> hub -> server and the metrics sampler into one service.

Kept separate from cli.py so revision 1 can drive the same service from a web UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from vinylcast.config import Config
from vinylcast.encoder import Encoder, build_command
from vinylcast.framing import make_framer
from vinylcast.hub import Hub
from vinylcast.metrics import Metrics, Sampler
from vinylcast.server import StreamServer, stream_url


class Service:
    def __init__(self, cfg: Config, *, sample_interval_s: float = 5.0, **encoder_kw: Any) -> None:
        self.cfg = cfg
        self.metrics = Metrics(cfg.metrics_path)
        self.hub = Hub(cfg.client_max_buffer_s, self.metrics)
        self.framer = make_framer(cfg.codec, self._on_flac_header)
        self.encoder = Encoder(
            build_command(cfg),
            self._on_chunk,
            self.metrics,
            on_start=self._on_encoder_start,
            **encoder_kw,
        )
        self.server = StreamServer(
            cfg, self.hub, self.metrics, self.encoder.status, lambda: self.framer.stream_header
        )
        self.sampler = Sampler(
            self.metrics, self.hub.stats, lambda: self.encoder.pid, sample_interval_s
        )
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
