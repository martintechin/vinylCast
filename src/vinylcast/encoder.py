"""ffmpeg subprocess lifecycle: stdout chunks, stderr parsing, restart with backoff.

One ffmpeg process at a time. Its stdout is read in chunks and handed to `sink`; its
stderr is logged to metrics line by line. `on_start` runs before each process's first
chunk so downstream state (framing) can reset. If ffmpeg exits or stdout stalls for longer
than `stall_timeout_s`, it is killed and restarted after a backoff delay. The sink and
whoever consumes it (hub, clients) are not touched by a restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Sequence
from enum import StrEnum
from typing import Any

from vinylcast import source
from vinylcast.config import Config
from vinylcast.metrics import Metrics

BACKOFF_S: tuple[float, ...] = (1, 2, 5, 10)
STALL_TIMEOUT_S = 2.0
STABLE_AFTER_S = 30.0  # a run this long resets the backoff sequence
CHUNK_SIZE = 8192
XRUN_MARKERS = ("xrun", "overrun", "underrun", "input/output error")


def codec_args(cfg: Config) -> list[str]:
    if cfg.codec == "mp3":
        # No ID3v2 tag and no Xing frame: the stream starts directly on an MP3 frame.
        return [
            "-c:a", "libmp3lame", "-b:a", cfg.mp3_bitrate,
            "-write_xing", "0", "-id3v2_version", "0",
            "-f", "mp3",
        ]  # fmt: skip
    if cfg.codec == "flac":
        # 16-bit like the UFO202, also for tone/file sources whose native format differs.
        return ["-sample_fmt", "s16", "-c:a", "flac", "-f", "flac"]
    raise ValueError(f"unknown codec {cfg.codec!r}")


def build_command(cfg: Config, ffmpeg: str = "ffmpeg") -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning",
        *source.input_args(cfg), *source.output_args(cfg), *codec_args(cfg),
        "pipe:1",
    ]  # fmt: skip


class EncoderState(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"
    STOPPED = "stopped"


class Encoder:
    def __init__(
        self,
        cmd: Sequence[str],
        sink: Callable[[bytes], None],
        metrics: Metrics,
        *,
        stall_timeout_s: float = STALL_TIMEOUT_S,
        backoff_s: Sequence[float] = BACKOFF_S,
        stable_after_s: float = STABLE_AFTER_S,
        chunk_size: int = CHUNK_SIZE,
        on_start: Callable[[], None] | None = None,
    ) -> None:
        self._cmd = list(cmd)
        self._sink = sink
        self._metrics = metrics
        self._stall_timeout_s = stall_timeout_s
        self._backoff_s = tuple(backoff_s)
        self._stable_after_s = stable_after_s
        self._chunk_size = chunk_size
        self._on_start = on_start
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = asyncio.Event()
        self.state = EncoderState.IDLE
        self.restarts = 0
        self.bytes_in = 0

    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None and proc.returncode is None else None

    def status(self) -> dict[str, Any]:
        return {"state": self.state.value, "pid": self.pid, "restarts": self.restarts}

    async def run(self) -> None:
        attempt = 0
        while not self._stopping.is_set():
            self.state = EncoderState.STARTING
            started = time.monotonic()
            reason, returncode = await self._run_once(attempt)
            if self._stopping.is_set():
                break
            ran_s = round(time.monotonic() - started, 2)
            self._metrics.event(f"encoder_{reason}", returncode=returncode, ran_s=ran_s)
            if ran_s >= self._stable_after_s:
                attempt = 0
            delay = self._backoff_s[min(attempt, len(self._backoff_s) - 1)]
            attempt += 1
            self.restarts += 1
            self._metrics.event("encoder_restart", delay_s=delay, restarts=self.restarts)
            self.state = EncoderState.BACKOFF
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), delay)
        self.state = EncoderState.STOPPED

    async def _run_once(self, attempt: int) -> tuple[str, int | None]:
        """Run one ffmpeg process until it exits, stalls or we stop. Returns (reason, rc)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            self._metrics.event("encoder_spawn_failed", error=str(e))
            return "exit", None
        self._proc = proc
        self._metrics.event("encoder_start", pid=proc.pid, attempt=attempt)
        if self._on_start is not None:
            self._on_start()  # e.g. framer reset: a new process starts a new stream
        stderr_task = asyncio.create_task(self._read_stderr(proc))
        try:
            reason = await self._pump(proc)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            returncode = await proc.wait()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stderr_task, 1.0)
            stderr_task.cancel()
        return reason, returncode

    async def _pump(self, proc: asyncio.subprocess.Process) -> str:
        assert proc.stdout is not None
        while True:
            try:
                data = await asyncio.wait_for(
                    proc.stdout.read(self._chunk_size), self._stall_timeout_s
                )
            except TimeoutError:
                return "stall"
            if not data:
                return "exit"
            self.state = EncoderState.RUNNING
            self.bytes_in += len(data)
            self._sink(data)

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            kind = (
                "ffmpeg_xrun" if any(m in line.lower() for m in XRUN_MARKERS) else "ffmpeg_warning"
            )
            self._metrics.event(kind, pid=proc.pid, line=line)

    async def stop(self) -> None:
        self._stopping.set()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
