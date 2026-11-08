"""One producer, N bounded per-client queues; slow clients are dropped, never waited for.

`publish` is synchronous and never awaits, so a stuck client cannot block the producer
or other clients. Each queue is bounded by the *audio duration* it holds rather than a
item count. Late joiners get no history: they start at the next published item.

Items are whole frames (from framing), each tagged with its exact audio duration.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from vinylcast.metrics import Metrics


class Subscriber:
    def __init__(self, sub_id: int, remote: str) -> None:
        self.id = sub_id
        self.remote = remote
        self.connected_at = time.monotonic()
        self.bytes_sent = 0
        self.closed_reason: str | None = None
        self.on_close: Callable[[], None] | None = None
        self._queue: deque[tuple[bytes, float]] = deque()
        self._queued_s = 0.0
        self._wakeup = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self.closed_reason is not None

    @property
    def queued_s(self) -> float:
        return self._queued_s

    def _put(self, data: bytes, duration_s: float) -> None:
        self._queue.append((data, duration_s))
        self._queued_s += duration_s
        self._wakeup.set()

    def _close(self, reason: str) -> None:
        if self.closed:
            return
        self.closed_reason = reason
        self._queue.clear()
        self._queued_s = 0.0
        self._wakeup.set()
        if self.on_close is not None:
            self.on_close()

    async def get(self) -> bytes | None:
        """Next item, or None once the subscriber has been closed."""
        while not self._queue:
            if self.closed:
                return None
            self._wakeup.clear()
            await self._wakeup.wait()
        if self.closed:
            return None
        data, duration_s = self._queue.popleft()
        self._queued_s = max(0.0, self._queued_s - duration_s)
        return data


class Hub:
    def __init__(self, max_buffer_s: float, metrics: Metrics) -> None:
        self.max_buffer_s = max_buffer_s
        self._metrics = metrics
        self._subs: dict[int, Subscriber] = {}
        self._ids = itertools.count(1)
        self.bytes_in = 0
        self.bytes_out = 0

    @property
    def client_count(self) -> int:
        return len(self._subs)

    def subscribe(self, remote: str = "") -> Subscriber:
        sub = Subscriber(next(self._ids), remote)
        self._subs[sub.id] = sub
        return sub

    def unsubscribe(self, sub: Subscriber, reason: str) -> None:
        """Remove a subscriber and log the disconnect. Idempotent."""
        if self._subs.pop(sub.id, None) is None:
            return
        sub._close(reason)
        self._metrics.event(
            "client_disconnect",
            client=sub.id,
            remote=sub.remote,
            reason=sub.closed_reason,
            bytes_sent=sub.bytes_sent,
            connected_s=round(time.monotonic() - sub.connected_at, 2),
            clients=self.client_count,
        )

    def publish(self, data: bytes, duration_s: float) -> None:
        self.bytes_in += len(data)
        for sub in list(self._subs.values()):
            if sub.queued_s + duration_s > self.max_buffer_s:
                self._metrics.event(
                    "client_slow", client=sub.id, remote=sub.remote, queued_s=round(sub.queued_s, 2)
                )
                sub._close("slow")
                self.unsubscribe(sub, "slow")
            else:
                sub._put(data, duration_s)

    def record_sent(self, sub: Subscriber, n: int) -> None:
        sub.bytes_sent += n
        self.bytes_out += n

    def close(self, reason: str = "shutdown") -> None:
        for sub in list(self._subs.values()):
            self.unsubscribe(sub, reason)

    def stats(self) -> dict[str, Any]:
        return {
            "clients": self.client_count,
            "bytes_in": self.bytes_in,
            "bytes_out": self.bytes_out,
        }
