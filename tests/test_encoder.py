import asyncio
import contextlib
import os
import signal
from pathlib import Path

import pytest
from conftest import events, requires_ffmpeg, wait_until

from vinylcast.config import load_config
from vinylcast.encoder import Encoder, EncoderState, build_command
from vinylcast.metrics import Metrics


class Collector:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    def __call__(self, data: bytes) -> None:
        self.chunks.append(data)

    @property
    def total(self) -> int:
        return sum(map(len, self.chunks))


@contextlib.asynccontextmanager
async def running(enc: Encoder):  # type: ignore[no-untyped-def]
    task = asyncio.create_task(enc.run())
    try:
        yield task
    finally:
        await enc.stop()
        await asyncio.wait_for(task, 5)


@requires_ffmpeg
async def test_tone_produces_mp3(tmp_path: Path) -> None:
    sink = Collector()
    enc = Encoder(build_command(load_config(overrides={"source": "tone"})), sink, Metrics(None))
    async with running(enc):
        await wait_until(lambda: sink.total > 20_000)
        assert enc.state == EncoderState.RUNNING and enc.pid is not None
        pid = enc.pid
    assert enc.state == EncoderState.STOPPED
    assert sink.chunks[0][:2] == b"\xff\xfb"  # MP3 frame sync, no ID3 tag
    with pytest.raises(ProcessLookupError):  # killed and reaped
        os.kill(pid, 0)


@requires_ffmpeg
async def test_killed_ffmpeg_is_restarted(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    sink = Collector()
    cmd = build_command(load_config(overrides={"source": "tone"}))
    enc = Encoder(cmd, sink, Metrics(path), backoff_s=(0.2,))
    async with running(enc):
        await wait_until(lambda: enc.state == EncoderState.RUNNING)
        first_pid = enc.pid
        assert first_pid is not None
        os.kill(first_pid, signal.SIGKILL)
        await wait_until(lambda: enc.pid not in (None, first_pid))
        before = sink.total
        await wait_until(lambda: sink.total > before + 10_000)
    (exit_ev,) = events(path, "encoder_exit")
    assert exit_ev["returncode"] == -signal.SIGKILL
    assert events(path, "encoder_restart")[0]["delay_s"] == 0.2
    assert len(events(path, "encoder_start")) == 2
    assert enc.restarts == 1


async def test_stall_triggers_restart(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    enc = Encoder(
        ["sleep", "30"], Collector(), Metrics(path), stall_timeout_s=0.3, backoff_s=(0.1,)
    )
    async with running(enc):
        await wait_until(lambda: enc.restarts >= 1)
    assert events(path, "encoder_stall")[0]["returncode"] == -signal.SIGKILL


async def test_backoff_sequence(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    enc = Encoder(["false"], Collector(), Metrics(path), backoff_s=(0.01, 0.02, 0.05))
    async with running(enc):
        await wait_until(lambda: enc.restarts >= 5)
    delays = [e["delay_s"] for e in events(path, "encoder_restart")][:5]
    assert delays == [0.01, 0.02, 0.05, 0.05, 0.05]


async def test_stderr_is_logged(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    cmd = ["sh", "-c", "echo 'ALSA buffer xrun.' >&2; echo 'something odd' >&2; exit 1"]
    enc = Encoder(cmd, Collector(), Metrics(path), backoff_s=(10,))
    async with running(enc):
        await wait_until(lambda: enc.restarts >= 1)
    assert events(path, "ffmpeg_xrun")[0]["line"] == "ALSA buffer xrun."
    assert events(path, "ffmpeg_warning")[0]["line"] == "something odd"


async def test_missing_binary_backs_off(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    enc = Encoder(["/nonexistent/ffmpeg"], Collector(), Metrics(path), backoff_s=(0.01,))
    async with running(enc):
        await wait_until(lambda: enc.restarts >= 2)
    assert events(path, "encoder_spawn_failed")


@requires_ffmpeg
async def test_ffmpeg_in_own_process_group() -> None:
    """Regression (Martin's Pi): Ctrl-C / systemd SIGINT hit ffmpeg too, so every clean
    shutdown was logged as encoder_exit + encoder_restart. ffmpeg must not share our
    process group."""
    sink = Collector()
    enc = Encoder(build_command(load_config(overrides={"source": "tone"})), sink, Metrics(None))
    async with running(enc):
        await wait_until(lambda: enc.pid is not None)
        assert enc.pid is not None
        assert os.getpgid(enc.pid) != os.getpgid(0)


async def test_device_busy_is_not_an_xrun(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    lines = [
        "[alsa @ 0x1] cannot open audio device hw:CARD=CODEC,DEV=0 (Device or resource busy)",
        "[in#0 @ 0x2] Error opening input: Input/output error",
        "[alsa @ 0x1] ALSA buffer xrun.",
    ]
    script = "; ".join(f"echo '{line}' >&2" for line in lines) + "; exit 1"
    enc = Encoder(["sh", "-c", script], Collector(), Metrics(path), backoff_s=(10,))
    async with running(enc):
        await wait_until(lambda: enc.restarts >= 1)
    assert [e["line"] for e in events(path, "ffmpeg_xrun")] == [lines[2]]
    assert len(events(path, "ffmpeg_warning")) == 2
