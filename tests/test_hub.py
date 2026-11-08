import asyncio
from pathlib import Path

from conftest import events

from vinylcast.hub import Hub
from vinylcast.metrics import Metrics


def make_hub(tmp_path: Path, max_s: float = 1.0) -> tuple[Hub, Path]:
    path = tmp_path / "m.jsonl"
    return Hub(max_s, Metrics(path)), path


async def test_fan_out(tmp_path: Path) -> None:
    hub, _ = make_hub(tmp_path)
    a, b = hub.subscribe("a"), hub.subscribe("b")
    hub.publish(b"x", 0.1)
    hub.publish(b"y", 0.1)
    assert [await a.get(), await a.get()] == [b"x", b"y"]
    assert [await b.get(), await b.get()] == [b"x", b"y"]


async def test_late_joiner_gets_no_history(tmp_path: Path) -> None:
    hub, _ = make_hub(tmp_path)
    hub.subscribe("early")
    hub.publish(b"old", 0.1)
    late = hub.subscribe("late")
    hub.publish(b"new", 0.1)
    assert await late.get() == b"new"


async def test_get_waits_for_data(tmp_path: Path) -> None:
    hub, _ = make_hub(tmp_path)
    sub = hub.subscribe()
    task = asyncio.create_task(sub.get())
    await asyncio.sleep(0.01)
    assert not task.done()
    hub.publish(b"z", 0.1)
    assert await asyncio.wait_for(task, 1) == b"z"


async def test_slow_client_dropped_others_unaffected(tmp_path: Path) -> None:
    hub, path = make_hub(tmp_path, max_s=1.0)
    slow, fast = hub.subscribe("slow"), hub.subscribe("fast")
    closed: list[bool] = []
    slow.on_close = lambda: closed.append(True)
    received = []
    for i in range(30):  # 3 s of audio; publish never awaits
        hub.publish(bytes([i]), 0.1)
        received.append(await fast.get())
    assert received == [bytes([i]) for i in range(30)]
    assert slow.closed_reason == "slow" and closed == [True]
    assert await slow.get() is None
    assert hub.client_count == 1
    (drop,) = events(path, "client_disconnect")
    assert drop["reason"] == "slow" and drop["remote"] == "slow"
    assert events(path, "client_slow")[0]["queued_s"] <= 1.0


async def test_queue_limit_is_duration_based(tmp_path: Path) -> None:
    hub, _ = make_hub(tmp_path, max_s=1.0)
    sub = hub.subscribe()
    for _ in range(10):
        hub.publish(b"x" * 100_000, 0.1)  # 1.0 s total: exactly at the limit, not over
    assert not sub.closed
    hub.publish(b"x", 0.1)
    assert sub.closed_reason == "slow"


async def test_close_wakes_waiters(tmp_path: Path) -> None:
    hub, path = make_hub(tmp_path)
    sub = hub.subscribe()
    task = asyncio.create_task(sub.get())
    await asyncio.sleep(0.01)
    hub.close("shutdown")
    assert await asyncio.wait_for(task, 1) is None
    assert events(path, "client_disconnect")[0]["reason"] == "shutdown"


async def test_unsubscribe_idempotent(tmp_path: Path) -> None:
    hub, path = make_hub(tmp_path)
    sub = hub.subscribe()
    hub.unsubscribe(sub, "client_gone")
    hub.unsubscribe(sub, "client_gone")
    assert len(events(path, "client_disconnect")) == 1
