"""aiohttp server: GET/HEAD /stream.mp3 and /stream.flac, GET /health.

Only the active codec's path answers 200; the other returns 404. Stream responses are
200 with no Content-Length and `Cache-Control: no-cache`; Range headers are ignored.
Every request is logged with method, path, remote address and all headers, because the
Cast receiver's actual HTTP behaviour is one of the things the PoC has to discover.

Note: aiohttp sends HTTP/1.1 responses without Content-Length as
`Transfer-Encoding: chunked` (HTTP/1.0 requests get a close-delimited body).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import web

from vinylcast.config import Config
from vinylcast.hub import Hub
from vinylcast.metrics import Metrics

CONTENT_TYPES = {"mp3": "audio/mpeg", "flac": "audio/flac"}

# Without these, Linux grows a stalled connection's send buffer up to tcp_wmem max (4 MB,
# ~100 s of 320k MP3), so writes never block and the hub never sees the client lag.
# Linux doubles SO_SNDBUF; together with aiohttp's 64 KiB drain interval, at most
# ~128 KiB (~3 s of 320k MP3) can sit in buffers before the hub queue starts to grow.
SEND_BUFFER_BYTES = 32 * 1024
WRITE_HIGH_WATER_BYTES = 16 * 1024

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def detect_lan_ip() -> str:
    """IP of the interface on the default route. Connecting a UDP socket sends no packets."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1; only used for the routing lookup
            return str(s.getsockname()[0])
        except OSError:
            return "127.0.0.1"


def stream_url(cfg: Config) -> str:
    ip = cfg.advertise_ip or detect_lan_ip()
    return f"http://{ip}:{cfg.http_port}/stream.{cfg.codec}"


def _limit_send_buffering(transport: asyncio.Transport) -> None:
    sock = transport.get_extra_info("socket")
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SEND_BUFFER_BYTES)
    transport.set_write_buffer_limits(high=WRITE_HIGH_WATER_BYTES)


class StreamServer:
    def __init__(
        self,
        cfg: Config,
        hub: Hub,
        metrics: Metrics,
        encoder_status: Callable[[], dict[str, Any]],
        stream_header: Callable[[], bytes | None] = lambda: None,
    ) -> None:
        self._cfg = cfg
        self._hub = hub
        self._metrics = metrics
        self._encoder_status = encoder_status
        self._stream_header = stream_header
        self._started = time.monotonic()
        self._runner: web.AppRunner | None = None
        self.port: int | None = None

        self.app = web.Application(middlewares=[self._log_request])
        self.app.router.add_get("/stream.mp3", self._stream)  # add_get also registers HEAD
        self.app.router.add_get("/stream.flac", self._stream)
        self.app.router.add_get("/health", self._health)

    def set_config(self, cfg: Config) -> None:
        """Codec (which stream path answers) and other settings may change at runtime."""
        self._cfg = cfg

    @web.middleware
    async def _log_request(self, request: web.Request, handler: Handler) -> web.StreamResponse:
        if request.method in ("GET", "HEAD") and request.path.startswith("/api/"):
            return await handler(request)  # UI polling/SSE: not receiver behaviour, not logged
        self._metrics.event(
            "http_request",
            method=request.method,
            path=request.path_qs,
            remote=request.remote,
            version=f"{request.version.major}.{request.version.minor}",
            headers=dict(request.headers),
        )
        return await handler(request)

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "codec": self._cfg.codec,
                "encoder": self._encoder_status(),
                "uptime_s": round(time.monotonic() - self._started, 1),
                **self._hub.stats(),
            }
        )

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        codec = request.path.rsplit(".", 1)[-1]
        if codec != self._cfg.codec:
            raise web.HTTPNotFound()
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": CONTENT_TYPES[codec], "Cache-Control": "no-cache"},
        )
        await resp.prepare(request)
        if request.method == "HEAD":
            return resp

        remote = request.remote or ""
        sub = self._hub.subscribe(remote)
        transport = request.transport
        if transport is not None:
            _limit_send_buffering(transport)
            # Hub drops (slow client, shutdown) must also unblock a write stuck on a full socket.
            sub.on_close = transport.abort
        self._metrics.event(
            "client_connect", client=sub.id, remote=remote, clients=self._hub.client_count
        )
        reason = "client_gone"
        try:
            first = await sub.get()
            if first is not None:
                # FLAC: a mid-stream joiner needs the cached stream header before its first
                # frame. Fetched after the first frame arrives, so it is always captured.
                header = self._stream_header()
                if header:
                    await resp.write(header)
                    self._hub.record_sent(sub, len(header))
            data = first
            while data is not None:
                await resp.write(data)
                self._hub.record_sent(sub, len(data))
                data = await sub.get()
        except ConnectionError:
            pass  # includes aiohttp's ClientConnectionResetError
        finally:
            sub.on_close = None
            self._hub.unsubscribe(sub, sub.closed_reason or reason)
        return resp

    async def start(self, host: str = "0.0.0.0", port: int | None = None) -> int:
        self._runner = web.AppRunner(self.app, access_log=None, shutdown_timeout=2.0)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner, host, self._cfg.http_port if port is None else port, reuse_address=True
        )
        await site.start()
        self.port = int(self._runner.addresses[0][1])
        self._metrics.event("server_start", host=host, port=self.port)
        return self.port

    async def stop(self) -> None:
        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
            self._runner = None
