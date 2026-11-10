"""Wires source -> encoder -> hub -> server and the metrics sampler into one service.

Kept separate from cli.py so revision 1 can drive the same service from a web UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable
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
        self._encoder_kw = encoder_kw
        self.level_listeners: list[Callable[[Level], None]] = []
        self.metrics = Metrics(cfg.metrics_path)
        self.hub = Hub(cfg.client_max_buffer_s, self.metrics)
        self.framer = make_framer(cfg.codec, self._on_flac_header)
        self.level_reader = LevelReader()
        self.peaks = PeakLogger(self.metrics, cfg.peak_log_s)
        self.latest_level: Level | None = None
        self.encoder = self._make_encoder(cfg)
        self._encoder_task: asyncio.Task[None] | None = None
        self.server = StreamServer(
            cfg,
            self.hub,
            self.metrics,
            lambda: self.encoder.status(),  # the encoder object is replaced on reconfigure
            lambda: self.framer.stream_header,
        )
        self.sampler = Sampler(
            self.metrics, self.hub.stats, lambda: self.encoder.pid, sample_interval_s
        )
        self.health = PiHealth(self.metrics)
        self._tasks: list[asyncio.Task[None]] = []

    def _make_encoder(self, cfg: Config) -> Encoder:
        return Encoder(
            build_command(cfg),
            self._on_chunk,
            self.metrics,
            on_start=self._on_encoder_start,
            on_info=self._on_ffmpeg_info,
            **self._encoder_kw,
        )

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
            for listener in self.level_listeners:
                listener(level)

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
        self._encoder_task = asyncio.create_task(self.encoder.run(), name="encoder")
        self._tasks = [
            asyncio.create_task(self.sampler.run(), name="sampler"),
            asyncio.create_task(self.health.run(), name="pi-health"),
        ]
        return bound

    async def reconfigure(self, cfg: Config) -> list[str]:
        """Apply a new config without restarting the process. Settings that change the stream
        (codec, bitrate, source, rate, measurement) restart the encoder and disconnect all
        listeners; the caller re-casts. Returns the names of the stream settings that changed
        (empty: nothing had to be restarted)."""
        keys = ("codec", "mp3_bitrate", "source", "alsa_device", "source_file", "tone_beep_s",
                "sample_rate", "measure_levels")  # fmt: skip
        changed = [k for k in keys if getattr(cfg, k) != getattr(self.cfg, k)]
        self.cfg = cfg
        self.peaks = PeakLogger(self.metrics, cfg.peak_log_s)
        self.server.set_config(cfg)
        if not changed:
            return []
        await self._stop_encoder()
        self.hub.close("reconfigure")
        if "codec" in changed:
            self.framer = make_framer(cfg.codec, self._on_flac_header)
        self.encoder = self._make_encoder(cfg)
        self._encoder_task = asyncio.create_task(self.encoder.run(), name="encoder")
        self.metrics.event("service_reconfigured", changed=changed, codec=cfg.codec)
        return changed

    async def _stop_encoder(self) -> None:
        await self.encoder.stop()
        if self._encoder_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._encoder_task, 10)
            self._encoder_task = None

    async def stop(self) -> None:
        await self._stop_encoder()
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


async def run_daemon(toml_path: Any, overrides: dict[str, Any]) -> int:
    """`daemon` (revision 1): always capture and measure; cast to the last speaker when music
    is detected, stop after `stop_after_silence_s` of silence. Settings changed in the UI
    (state.json) override the TOML; command-line flags override both."""
    from dataclasses import replace

    from vinylcast.cast import CastSession, DeviceBrowser
    from vinylcast.config import ConfigError, load_config
    from vinylcast.driver import Driver
    from vinylcast.state import StateStore
    from vinylcast.web import WebApi

    base = load_config(toml_path, overrides)
    store = StateStore(base.state_path)
    state = store.load()
    try:
        cfg = load_config(toml_path, overrides, state.settings)
    except ConfigError as e:
        print(f"daemon: ignoring invalid saved settings ({e})", file=sys.stderr)
        cfg, state.settings = base, {}
    cfg = replace(cfg, measure_levels=True)  # detection needs the level readings
    service = Service(cfg)
    service.metrics.event("service_start", config=cfg.__dict__, mode="daemon",
                          last_target=state.last_target)  # fmt: skip
    driver = Driver(
        service, store, state, service.metrics,
        lambda name, on_end: CastSession(name, service.metrics, on_end),
        toml_path=toml_path, cli_overrides={**overrides, "measure_levels": True},
    )  # fmt: skip
    service.level_listeners.append(driver.on_level)
    browser = DeviceBrowser()
    WebApi(service, driver, browser.devices).install(service.server.app)  # before start
    if not await _start_or_report(service):
        return 1
    browser.start()
    led_task = None
    if cfg.status_led:
        from vinylcast.statusled import StatusLed, led_state

        def current_led() -> str:
            health = service.metrics.latest.get("health") or {}
            return led_state(driver.status(), service.encoder.status()["state"],
                             health.get("throttled"))  # fmt: skip

        led = StatusLed(cfg.led_pins, current_led, service.metrics, brightness=cfg.led_brightness)
        led_task = asyncio.create_task(led.run())
    target = driver.controller.target
    print(f"Listening on {cfg.source}; on needle drop: cast to {target or '(no speaker yet)'} "
          f"at {stream_url(cfg)}  (Ctrl-C to stop)", flush=True)  # fmt: skip
    print(f"Web API: http://{stream_url(cfg).split('/')[2]}/api/status", flush=True)
    task = asyncio.create_task(driver.run())
    try:
        await _wait_for_signal()
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(driver.shutdown(), 15)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if led_task is not None:
            led_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await led_task
        await asyncio.to_thread(browser.stop)
        await service.stop()
    return 0
