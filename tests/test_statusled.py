"""Status LED: Martin's mapping (pure) and the gpiozero driver on mock PWM pins."""

import asyncio
from pathlib import Path

import pytest
from conftest import events, wait_until

from vinylcast.metrics import Metrics
from vinylcast.statusled import PATTERNS, StatusLed, led_state


def st(state: str = "idle", hold: str | None = None, recovering: bool = False) -> dict:
    return {"state": state, "hold": hold, "recovering": recovering}


@pytest.mark.parametrize(
    ("status", "encoder", "throttled", "expected"),
    [
        (st(), "running", "0x0", "idle"),  # listening, no signal
        (st("starting"), "running", "0x0", "starting"),
        (st("playing"), "running", "0x0", "playing"),
        (st("stopping"), "running", "0x0", "playing"),
        (st(hold="manual"), "running", "0x0", "hold"),
        (st(hold="taken_over"), "running", "0x0", "hold"),
        (st(hold="busy"), "running", "0x0", "warn"),
        (st(hold="unavailable"), "running", "0x0", "warn"),
        (st(recovering=True), "running", "0x0", "warn"),  # reconnecting to the speaker
        (st(hold="errors"), "running", "0x0", "error"),  # recovery gave up
        (st("playing"), "backoff", "0x0", "error"),  # encoder down
        (st("playing"), "running", "0x1", "error"),  # under-voltage right now
        (st("playing"), "running", "0x50000", "playing"),  # only "since boot": not an error
        (st(), "running", None, "idle"),  # no vcgencmd (dev machine)
    ],
)
def test_mapping(status: dict, encoder: str, throttled: str | None, expected: str) -> None:
    assert led_state(status, encoder, throttled) == expected


@pytest.fixture
def mock_factory():  # type: ignore[no-untyped-def]
    gpiozero = pytest.importorskip("gpiozero")
    from gpiozero.pins.mock import MockFactory, MockPWMPin

    factory = MockFactory(pin_class=MockPWMPin)
    yield factory
    factory.reset()
    gpiozero.Device.pin_factory = None


def pin_values(factory, pins=(17, 22, 23)) -> tuple:  # type: ignore[no-untyped-def]
    return tuple(round(factory.pin(p).state, 3) for p in pins)


async def test_driver_shows_patterns_and_releases_pins(tmp_path: Path, mock_factory) -> None:  # type: ignore[no-untyped-def]
    current = {"name": "idle"}
    led = StatusLed((17, 22, 23), lambda: current["name"], Metrics(tmp_path / "m.jsonl"),
                    pin_factory=mock_factory, poll_s=0.01)  # fmt: skip
    task = asyncio.create_task(led.run())
    await wait_until(lambda: led.current == "idle")
    assert pin_values(mock_factory) == PATTERNS["idle"][0]  # dim white
    current["name"] = "playing"
    await wait_until(lambda: led.current == "playing")
    assert pin_values(mock_factory) == (0.0, 0.1, 0.0)  # dim green
    current["name"] = "starting"
    await wait_until(lambda: led.current == "starting")
    seen = set()
    for _ in range(30):  # blinking: blue on and off within ~1 s
        seen.add(pin_values(mock_factory))
        await asyncio.sleep(0.05)
    assert (0.0, 0.0, 1.0) in seen and (0.0, 0.0, 0.0) in seen
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert led.led.closed  # pins released -> LED off
    assert events(tmp_path / "m.jsonl", "status_led")[0]["pins"] == [17, 22, 23]


async def test_brightness_scales_colours(tmp_path: Path, mock_factory) -> None:  # type: ignore[no-untyped-def]
    led = StatusLed((17, 22, 23), lambda: "error", Metrics(tmp_path / "m.jsonl"),
                    brightness=0.3, pin_factory=mock_factory, poll_s=0.01)  # fmt: skip
    task = asyncio.create_task(led.run())
    await wait_until(lambda: led.current == "error")
    assert pin_values(mock_factory) == (0.3, 0.0, 0.0)
    task.cancel()


async def test_no_gpio_is_not_fatal(tmp_path: Path) -> None:
    """E.g. lgpio missing or no permission: the service keeps running without the LED."""

    class Broken:
        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            raise RuntimeError("no GPIO here")

    pytest.importorskip("gpiozero")
    led = StatusLed((17, 22, 23), lambda: "idle", Metrics(tmp_path / "m.jsonl"),
                    pin_factory=Broken())  # fmt: skip
    await asyncio.wait_for(led.run(), 2)  # returns instead of raising
    assert events(tmp_path / "m.jsonl", "status_led_error")
