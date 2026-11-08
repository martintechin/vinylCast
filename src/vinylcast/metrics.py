"""JSONL event and periodic sample log.

Every line is one JSON object with `ts` (unix time), `kind` ("event" or "sample") and,
for events, `event` (the event name). Events are also echoed to the `vinylcast` logger.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psutil

log = logging.getLogger("vinylcast")


class Metrics:
    def __init__(self, path: str | Path | None) -> None:
        # strftime codes give one file per run, e.g. "soak-%Y%m%d-%H%M.jsonl"
        self.path = Path(time.strftime(str(path))) if path is not None else None
        self._fh = self.path.open("a", buffering=1, encoding="utf-8") if self.path else None
        self._lock = threading.Lock()  # pychromecast callbacks log from their own threads
        # In memory for the web UI's diagnostics: recent events, latest record per kind.
        self.recent: deque[dict[str, Any]] = deque(maxlen=200)
        self.latest: dict[str, dict[str, Any]] = {}

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            if record.get("kind") == "event":
                self.recent.append(record)
            self.latest[str(record.get("kind"))] = record
            if self._fh is not None:
                self._fh.write(line)

    def event(self, name: str, **fields: Any) -> None:
        self._write({"ts": round(time.time(), 3), "kind": "event", "event": name, **fields})
        log.info("%s %s", name, " ".join(f"{k}={v}" for k, v in fields.items()))

    def sample(self, **fields: Any) -> None:
        self.write_record("sample", **fields)

    def write_record(self, kind: str, **fields: Any) -> None:
        self._write({"ts": round(time.time(), 3), "kind": kind, **fields})

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


async def _run(*cmd: str, timeout_s: float = 5.0) -> str | None:
    """Output of a small system command, or None if it is missing, fails or hangs."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return None
    return out.decode(errors="replace") if proc.returncode == 0 else None


def default_route_interface(route_table: str | None = None) -> str | None:
    """Interface of the IPv4 default route, from /proc/net/route."""
    if route_table is None:
        try:
            route_table = Path("/proc/net/route").read_text()
        except OSError:
            return None
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 1 and fields[1] == "00000000":
            return fields[0]
    return None


def is_wireless(iface: str) -> bool:
    return Path(f"/sys/class/net/{iface}/wireless").exists()


def parse_throttled(text: str | None) -> int | None:
    """`vcgencmd get_throttled` -> 'throttled=0x50005' -> 0x50005."""
    if not text or "=" not in text:
        return None
    try:
        return int(text.strip().split("=", 1)[1], 16)
    except ValueError:
        return None


_THROTTLE_BITS = {
    0: "under-voltage",
    1: "arm-frequency-capped",
    2: "throttled",
    3: "soft-temperature-limit",
}


def decode_throttled(value: int) -> dict[str, list[str]]:
    """vcgencmd get_throttled bits: 0-3 = happening now, 16-19 = has occurred since boot."""
    return {
        "now": [n for b, n in _THROTTLE_BITS.items() if value & (1 << b)],
        "since_boot": [n for b, n in _THROTTLE_BITS.items() if value & (1 << (b + 16))],
    }


def parse_iw_link(text: str | None) -> dict[str, Any] | None:
    """`iw dev <if> link` -> {connected, freq_mhz, signal_dbm, tx_mbit, rx_mbit}."""
    if text is None:
        return None
    if text.strip().startswith("Not connected"):
        return {"connected": False}
    out: dict[str, Any] = {"connected": True}
    for raw in text.splitlines():
        key, _, value = raw.strip().partition(":")
        value = value.strip()
        with contextlib.suppress(ValueError, IndexError):
            if key == "freq":
                out["freq_mhz"] = float(value.split()[0])
            elif key == "signal":
                out["signal_dbm"] = int(value.split()[0])
            elif key == "tx bitrate":
                out["tx_mbit"] = float(value.split()[0])
            elif key == "rx bitrate":
                out["rx_mbit"] = float(value.split()[0])
    return out


class PiHealth:
    """Every `interval_s`: `vcgencmd get_throttled` (Pi only) and, when the default route is
    Wi-Fi, the link's signal and bitrates. Writes a `health` record each time, and a
    `pi_throttled` event for any non-zero value (under-voltage / thermal throttling must show
    up in the data). Missing tools (dev machine) just give nulls."""

    def __init__(self, metrics: Metrics, interval_s: float = 60.0) -> None:
        self._metrics = metrics
        self._interval_s = interval_s

    async def collect(self) -> dict[str, Any]:
        throttled = parse_throttled(await _run("vcgencmd", "get_throttled"))
        iface = default_route_interface()
        wifi = None
        if iface and is_wireless(iface):
            wifi = parse_iw_link(await _run("iw", "dev", iface, "link"))
            ps = await _run("iw", "dev", iface, "get", "power_save")
            if wifi is not None and ps is not None:
                wifi["power_save"] = ps.strip().endswith("on")
        return {
            "throttled": None if throttled is None else hex(throttled),
            "net_iface": iface,
            "wifi": wifi,
        }

    async def run(self) -> None:
        last: str | None = "0x0"
        while True:
            health = await self.collect()
            self._metrics.write_record("health", **health)
            value = health["throttled"]
            # The "has occurred" bits stay set until reboot: log on change, not every minute.
            if value is not None and value != last and value != "0x0":
                self._metrics.event("pi_throttled", value=value, **decode_throttled(int(value, 16)))
            if value is not None:
                last = value
            await asyncio.sleep(self._interval_s)
