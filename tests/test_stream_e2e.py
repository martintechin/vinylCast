"""Real tone -> ffmpeg -> framing -> hub -> HTTP, for both codecs (short versions of the
M2 acceptance checks; the full 30 s join / 60 s record run is done from the CLI)."""

import asyncio
import os
import signal
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
from conftest import events, requires_ffmpeg, wait_until

from vinylcast.config import load_config
from vinylcast.encoder import EncoderState
from vinylcast.service import Service

pytestmark = requires_ffmpeg


@pytest.fixture(params=["mp3", "flac"])
async def svc(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Service]:
    cfg = load_config(
        overrides={
            "source": "tone",
            "codec": request.param,
            "metrics_path": str(tmp_path / "m.jsonl"),
        }
    )
    service = Service(cfg, sample_interval_s=0.5, backoff_s=(0.2,))
    service.port = await service.start(port=0)  # type: ignore[attr-defined]
    yield service
    await service.stop()


async def record(url: str, path: Path, seconds: float) -> None:
    loop = asyncio.get_running_loop()
    async with aiohttp.ClientSession() as s, s.get(url) as r:
        assert r.status == 200
        end = loop.time() + seconds
        with path.open("wb") as f:
            while loop.time() < end:
                f.write(await r.content.readany())


def decode_errors(path: Path) -> list[str]:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    return out.stderr.splitlines()


async def test_late_joiner_decodes_cleanly(svc: Service, tmp_path: Path) -> None:
    codec = svc.cfg.codec
    url = f"http://127.0.0.1:{svc.port}/stream.{codec}"  # type: ignore[attr-defined]
    await asyncio.sleep(3)  # join mid-stream
    rec = tmp_path / f"rec.{codec}"
    await record(url, rec, 4)
    assert decode_errors(rec) == []
    data = rec.read_bytes()
    if codec == "flac":
        assert data.startswith(b"fLaC") and svc.framer.stream_header is not None
        assert data.startswith(svc.framer.stream_header)
    else:
        assert data[:2] == b"\xff\xfb"  # starts on a frame, no ID3
    assert svc.framer.skipped_bytes == 0


async def test_restart_keeps_clients_and_header(svc: Service, tmp_path: Path) -> None:
    codec = svc.cfg.codec
    url = f"http://127.0.0.1:{svc.port}/stream.{codec}"  # type: ignore[attr-defined]
    rec = tmp_path / f"rec.{codec}"
    task = asyncio.create_task(record(url, rec, 5))
    await wait_until(lambda: svc.hub.client_count == 1 and svc.encoder.pid is not None)
    await asyncio.sleep(1)
    pid = svc.encoder.pid
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    await wait_until(lambda: svc.encoder.pid not in (None, pid))
    await wait_until(lambda: svc.encoder.state == EncoderState.RUNNING)
    await task
    path = Path(svc.cfg.metrics_path)
    assert events(path, "encoder_restart")
    assert not [e for e in events(path, "client_disconnect") if e["reason"] != "client_gone"]
    if codec == "flac":
        statuses = [e["status"] for e in events(path, "flac_header")]
        assert statuses == ["captured", "unchanged"]
    # The recording spans the restart. MP3 has no frame numbers and decodes cleanly. FLAC
    # frames carry a frame number that restarts at 0 in the new ffmpeg process, so a client
    # that stays connected sees timestamps jump backwards. The audio itself decodes; only
    # timestamp complaints appear. Open decision for Martin (see M2 report).
    errors = decode_errors(rec)
    if codec == "mp3":
        assert errors == []
    else:
        assert errors and all("non monotonically increasing dts" in e for e in errors)


@pytest.fixture
async def svc_peaks(tmp_path: Path) -> AsyncIterator[Service]:
    cfg = load_config(overrides={"source": "tone", "measure_levels": True, "peak_log_s": 1,
                                 "metrics_path": str(tmp_path / "m.jsonl")})  # fmt: skip
    service = Service(cfg, sample_interval_s=0.5)
    service.port = await service.start(port=0)  # type: ignore[attr-defined]
    yield service
    await service.stop()


async def test_stream_with_peak_logging(svc_peaks: Service, tmp_path: Path) -> None:
    """Peak measurement must not hold back the stream: steady data and clean decode."""
    url = f"http://127.0.0.1:{svc_peaks.port}/stream.mp3"  # type: ignore[attr-defined]
    rec = tmp_path / "rec.mp3"
    await record(url, rec, 4)
    assert rec.stat().st_size > 3 * 40_000 * 0.8  # ~40 kB/s at 320k
    assert decode_errors(rec) == []
    path = Path(svc_peaks.cfg.metrics_path)
    peaks = events(path, "input_peak")
    assert len(peaks) >= 2 and peaks[0]["peak_db"] == pytest.approx(-18.06, abs=0.1)
    assert peaks[0]["readings"] >= 9  # ~10 readings/s (100 ms blocks)
    assert svc_peaks.latest_level is not None
    assert events(path, "encoder_restart") == [] and events(path, "ffmpeg_warning") == []
