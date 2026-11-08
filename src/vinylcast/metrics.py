"""JSONL event and periodic sample log.

Every line is one JSON object with `ts` (unix time), `kind` ("event" or "sample") and,
for events, `event` (the event name). Events are also echoed to the `vinylcast` logger.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psutil

log = logging.getLogger("vinylcast")


class Metrics:
    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path is not None else None
        self._fh = self.path.open("a", buffering=1, encoding="utf-8") if self.path else None
        self._lock = threading.Lock()  # pychromecast callbacks log from their own threads

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            if self._fh is not None:
                self._fh.write(line)

    def event(self, name: str, **fields: Any) -> None:
        self._write({"ts": round(time.time(), 3), "kind": "event", "event": name, **fields})
        log.info("%s %s", name, " ".join(f"{k}={v}" for k, v in fields.items()))

    def sample(self, **fields: Any) -> None:
        self._write({"ts": round(time.time(), 3), "kind": "sample", **fields})

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


def read_records(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def _proc_stats(proc: psutil.Process | None, fresh: bool = False) -> dict[str, Any] | None:
    """`fresh`: the process was first seen this interval, so its CPU % is not yet meaningful
    (psutil's first cpu_percent call always returns 0.0) and is reported as None."""
    if proc is None:
        return None
    try:
        with proc.oneshot():
            cpu = proc.cpu_percent(None)
            return {
                "pid": proc.pid,
                "cpu_pct": None if fresh else cpu,
                "rss_mb": round(proc.memory_info().rss / 1e6, 1),
            }
    except psutil.Error:
        return None


class Sampler:
    """Writes a sample every `interval_s`: CPU % and RSS for Python and ffmpeg, plus
    whatever `state` returns (client count, bytes out, ...)."""

    def __init__(
        self,
        metrics: Metrics,
        state: Callable[[], dict[str, Any]],
        ffmpeg_pid: Callable[[], int | None],
        interval_s: float = 5.0,
    ) -> None:
        self._metrics = metrics
        self._state = state
        self._ffmpeg_pid = ffmpeg_pid
        self._interval_s = interval_s
        self._self = psutil.Process(os.getpid())
        self._ffmpeg: psutil.Process | None = None

    def _ffmpeg_proc(self) -> tuple[psutil.Process | None, bool]:
        """Current ffmpeg process and whether it is new since the last call."""
        pid = self._ffmpeg_pid()
        if pid is None:
            self._ffmpeg = None
        elif self._ffmpeg is None or self._ffmpeg.pid != pid:
            try:
                self._ffmpeg = psutil.Process(pid)
            except psutil.Error:
                self._ffmpeg = None
            return self._ffmpeg, True
        return self._ffmpeg, False

    async def run(self) -> None:
        self._self.cpu_percent(None)
        _proc_stats(self._ffmpeg_proc()[0])  # prime CPU counters if ffmpeg is already up
        while True:
            await asyncio.sleep(self._interval_s)
            ffmpeg, fresh = self._ffmpeg_proc()
            self._metrics.sample(
                python=_proc_stats(self._self),
                ffmpeg=_proc_stats(ffmpeg, fresh),
                **self._state(),
            )
