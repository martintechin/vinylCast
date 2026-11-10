"""Status LED (RGB on three GPIO pins) showing what VinylCast is doing.

Martin's mapping (2026-11-10):

| Condition                                         | LED                 |
| ------------------------------------------------- | ------------------- |
| idle, listening, no signal                        | dim white, solid    |
| starting (signal detected, casting)               | blue, blinking      |
| playing                                           | green, dim, solid   |
| manual hold (stopped by hand / app took over)     | blue, dim, solid    |
| speaker busy, unavailable, or reconnecting        | yellow, blinking    |
| error (encoder down, gave up, under-voltage now)  | red, solid          |
| service not running                               | off (pins released) |

`led_state` is pure (tested without hardware); `StatusLed` drives gpiozero's RGBLED (PWM, so
colours can be dim) and is only imported when `status_led = true`. On full Cast receivers
(Xiaomi, groups) "playing" starts when the speaker reports PLAYING, ~10 s before the sound.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from typing import Any

from vinylcast.metrics import Metrics

Color = tuple[float, float, float]

# name -> (colour, blinking)
PATTERNS: dict[str, tuple[Color, bool]] = {
    "idle": ((0.1, 0.1, 0.1), False),
    "starting": ((0.0, 0.0, 1.0), True),
    "playing": ((0.0, 0.1, 0.0), False),
    "hold": ((0.0, 0.0, 0.1), False),
    "warn": ((1.0, 0.6, 0.0), True),
    "error": ((1.0, 0.0, 0.0), False),
}
BLINK_S = 0.5
POLL_S = 0.5
# vcgencmd get_throttled bits that mean "right now": 0 under-voltage, 2 throttled.
THROTTLED_NOW = 0x1 | 0x4


def led_state(status: dict[str, Any], encoder_state: str | None, throttled: str | None) -> str:
    """Which pattern to show, highest priority first."""
    throttled_now = False
    if throttled:
        with contextlib.suppress(ValueError):
            throttled_now = bool(int(throttled, 16) & THROTTLED_NOW)
    hold = status.get("hold")
    state = status.get("state")
    if hold == "errors" or encoder_state == "backoff" or throttled_now:
        return "error"
    if hold in ("busy", "unavailable") or status.get("recovering"):
        return "warn"
    if state == "starting":
        return "starting"
    if state in ("playing", "stopping"):
        return "playing"
    if hold in ("manual", "taken_over"):
        return "hold"
    return "idle"


class StatusLed:
    def __init__(
        self,
        pins: Sequence[int],
        state: Callable[[], str],
        metrics: Metrics,
        *,
        brightness: float = 1.0,
        pin_factory: Any = None,
        poll_s: float = POLL_S,
    ) -> None:
        self._pins = tuple(pins)
        self._state = state
        self._metrics = metrics
        self._brightness = brightness
        self._pin_factory = pin_factory
        self._poll_s = poll_s
        self.led: Any = None
        self.current: str | None = None

    def _open(self) -> bool:
        try:
            from gpiozero import RGBLED  # optional dependency: vinylcast[led]

            self.led = RGBLED(*self._pins, pwm=True, pin_factory=self._pin_factory)
        except Exception as e:  # missing package, no GPIO access, pins busy: never fatal
            self._metrics.event("status_led_error", error=repr(e), pins=list(self._pins))
            return False
        self._metrics.event("status_led", pins=list(self._pins), brightness=self._brightness)
        return True

    def show(self, name: str) -> None:
        color, blink = PATTERNS[name]
        c = tuple(round(v * self._brightness, 3) for v in color)
        if blink:
            self.led.blink(on_time=BLINK_S, off_time=BLINK_S, on_color=c, background=True)
        else:
            self.led.color = c
        self.current = name

    async def run(self) -> None:
        if not self._open():
            return
        try:
            while True:
                name = self._state()
                if name != self.current:
                    self.show(name)
                await asyncio.sleep(self._poll_s)
        finally:
            with contextlib.suppress(Exception):
                self.led.close()  # off, pins released
