import asyncio
import contextlib
import os
import signal
import socket
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import pytest
from conftest import events, requires_ffmpeg, wait_until

from vinylcast.config import load_config
from vinylcast.encoder import EncoderState
from vinylcast.hub import Hub
from vinylcast.metrics import Metrics
from vinylcast.server import StreamServer
from vinylcast.service import Service


@dataclass
class Rig:
    base: str
    port: int
    hub: Hub
    metrics_path: Path


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    """Server + hub fed by a real-time fake producer: 16 KiB tagged as 25 ms of audio, every
    25 ms (~650 kB/s). Tags must match the publish rate, or readers look slow and get dropped."""
    path = tmp_path / "m.jsonl"
    metrics = Metrics(path)
    cfg = load_config(overrides={"source": "tone", "client_max_buffer_s": 1.0})
    hub = Hub(cfg.client_max_buffer_s, metrics)
    server = StreamServer(cfg, hub, metrics, lambda: {"state": "fake"})
    port = await server.start(host="127.0.0.1", port=0)

    async def produce() -> None:
        i = 0
        while True:
            hub.publish(bytes([i % 256]) * 16384, 0.025)
            i += 1
            await asyncio.sleep(0.025)

    task = asyncio.create_task(produce())
    yield Rig(f"http://127.0.0.1:{port}", port, hub, path)
    task.cancel()
    hub.close()
    await server.stop()


async def test_head_and_headers(rig: Rig) -> None:
    async with aiohttp.ClientSession() as s, s.head(f"{rig.base}/stream.mp3") as r:
        assert r.status == 200
        assert r.headers["Content-Type"] == "audio/mpeg"
        assert r.headers["Cache-Control"] == "no-cache"
        assert "Content-Length" not in r.headers
    assert rig.hub.client_count == 0  # HEAD does not subscribe


async def test_get_ignores_range(rig: Rig) -> None:
    async with (
        aiohttp.ClientSession() as s,
        s.get(f"{rig.base}/stream.mp3", headers={"Range": "bytes=100-"}) as r,
    ):
        assert r.status == 200
        assert "Content-Length" not in r.headers
        assert len(await r.content.readexactly(100_000)) == 100_000


async def test_inactive_codec_is_404(rig: Rig) -> None:
    async with aiohttp.ClientSession() as s:
        for method in ("GET", "HEAD"):
            async with s.request(method, f"{rig.base}/stream.flac") as r:
                assert r.status == 404


async def test_health(rig: Rig) -> None:
    async with aiohttp.ClientSession() as s, s.get(f"{rig.base}/health") as r:
        body = await r.json()
    assert body["codec"] == "mp3"
    assert body["encoder"] == {"state": "fake"}
    assert {"clients", "uptime_s", "bytes_out"} <= body.keys()


async def test_requests_logged_with_headers(rig: Rig) -> None:
    async with aiohttp.ClientSession() as s:
        async with s.head(f"{rig.base}/stream.mp3", headers={"X-Probe": "1"}):
            pass
    (req,) = events(rig.metrics_path, "http_request")
    assert req["method"] == "HEAD" and req["path"] == "/stream.mp3"
    assert req["remote"] == "127.0.0.1" and req["headers"]["X-Probe"] == "1"


async def test_two_clients_one_killed(rig: Rig) -> None:
    async with aiohttp.ClientSession() as a, aiohttp.ClientSession() as b:
        ra = await a.get(f"{rig.base}/stream.mp3")
        rb = await b.get(f"{rig.base}/stream.mp3")
        await ra.content.readexactly(200_000)
        await rb.content.readexactly(200_000)
        ra.close()  # drop connection
        await wait_until(lambda: rig.hub.client_count == 1)
        assert len(await rb.content.readexactly(1_000_000)) == 1_000_000
        rb.close()
    await wait_until(lambda: rig.hub.client_count == 0)
    reasons = [e["reason"] for e in events(rig.metrics_path, "client_disconnect")]
    assert reasons[0] == "client_gone"


async def test_non_reading_client_dropped(rig: Rig) -> None:
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    await loop.sock_connect(sock, ("127.0.0.1", rig.port))
    await loop.sock_sendall(sock, b"GET /stream.mp3 HTTP/1.1\r\nHost: x\r\n\r\n")
    try:
        async with aiohttp.ClientSession() as s, s.get(f"{rig.base}/stream.mp3") as good:
            await wait_until(lambda: events(rig.metrics_path, "client_slow"), timeout=15)
            # The reading client is unaffected.
            assert len(await good.content.readexactly(500_000)) == 500_000
            assert rig.hub.client_count == 1
    finally:
        sock.close()
    slow = [e for e in events(rig.metrics_path, "client_disconnect") if e["reason"] == "slow"]
    assert len(slow) == 1


def decode_errors(path: Path) -> str:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
    ).stderr


@requires_ffmpeg
async def test_end_to_end_tone(tmp_path: Path) -> None:
    """Real tone -> ffmpeg -> hub -> HTTP. A client records the stream; ffmpeg decodes it
    cleanly; killing the encoder keeps the client connected and data flowing."""
    cfg = load_config(overrides={"source": "tone", "metrics_path": str(tmp_path / "m.jsonl")})
    svc = Service(cfg, sample_interval_s=0.5, backoff_s=(0.2,))
    port = await svc.start(port=0)
    try:
        url = f"http://127.0.0.1:{port}/stream.mp3"
        rec = tmp_path / "rec.mp3"
        async with aiohttp.ClientSession() as s, s.get(url) as r:
            with rec.open("wb") as f:
                for _ in range(10):  # ~3 s at 40 kB/s (plus ffmpeg's start-up burst)
                    f.write(await r.content.readexactly(12_000))
            assert decode_errors(rec) == ""
            pid = svc.encoder.pid
            assert pid is not None
            os.kill(pid, signal.SIGKILL)
            await wait_until(lambda: svc.encoder.pid not in (None, pid))
            await wait_until(lambda: svc.encoder.state == EncoderState.RUNNING)
            assert len(await asyncio.wait_for(r.content.readexactly(40_000), 5)) == 40_000
            assert svc.hub.client_count == 1
    finally:
        with contextlib.suppress(Exception):
            await svc.stop()
    path = tmp_path / "m.jsonl"
    assert events(path, "encoder_restart")
    # The client was never dropped by the restart; it left on close (or at shutdown, if the
    # server had not written again before stop()).
    (disc,) = events(path, "client_disconnect")
    assert disc["reason"] in ("client_gone", "shutdown")
    from vinylcast.metrics import read_records

    samples = [r for r in read_records(path) if r["kind"] == "sample"]
    assert samples and samples[0]["python"]["rss_mb"] > 0


async def test_stalled_client_dropped_at_real_rate(tmp_path: Path) -> None:
    """Regression: at real 320k rate, kernel send-buffer autotuning used to absorb ~100 s of
    audio, so a stalled client was never dropped. With per-connection limits it must be
    dropped within client_max_buffer_s plus a few seconds of socket buffering."""
    path = tmp_path / "m.jsonl"
    metrics = Metrics(path)
    cfg = load_config(overrides={"source": "tone", "client_max_buffer_s": 1.0})
    hub = Hub(cfg.client_max_buffer_s, metrics)
    server = StreamServer(cfg, hub, metrics, lambda: {})
    port = await server.start(host="127.0.0.1", port=0)

    async def produce() -> None:  # 40 kB/s in 1045-byte frames, like libmp3lame at 320k
        while True:
            hub.publish(b"\0" * 1045, 1045 / 40_000)
            await asyncio.sleep(1045 / 40_000)

    task = asyncio.create_task(produce())
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.setblocking(False)
    loop = asyncio.get_running_loop()
    try:
        await loop.sock_connect(sock, ("127.0.0.1", port))
        await loop.sock_sendall(sock, b"GET /stream.mp3 HTTP/1.1\r\nHost: x\r\n\r\n")
        start = loop.time()
        await wait_until(lambda: events(path, "client_slow"), timeout=15)
        assert loop.time() - start < 8
        (disc,) = events(path, "client_disconnect")
        assert disc["reason"] == "slow" and disc["bytes_sent"] < 250_000
    finally:
        sock.close()
        task.cancel()
        await server.stop()
