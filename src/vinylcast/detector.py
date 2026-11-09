"""Needle-drop detection: level readings -> `on` / `off` events with hysteresis.

Input is a stream of level readings (RMS and peak in dBFS, taken from ffmpeg's measurement;
where those come from is decided in R0 and does not matter here). RMS, not peak, decides,
so a single click cannot trigger. Timing:

- `on`:  RMS >= threshold continuously for `on_after_s` (default 1.5 s).
- `off`: RMS <  threshold - hysteresis continuously for `off_after_s` (default 1 s).

The long "stop after 180 s of silence" belongs to the controller; the detector only says
whether there is signal right now. Pure logic: time comes in with each reading.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Literal

Event = Literal["on", "off"]


@dataclass(frozen=True)
class Level:
    t: float  # monotonic seconds
    rms_db: float
    peak_db: float


class SignalDetector:
    def __init__(
        self,
        threshold_db: float,
        on_after_s: float = 1.5,
        off_after_s: float = 1.0,
        hysteresis_db: float = 6.0,
        history_s: float = 60.0,
    ) -> None:
        self.threshold_db = threshold_db
        self.on_after_s = on_after_s
        self.off_after_s = off_after_s
        self.hysteresis_db = hysteresis_db
        self.history_s = history_s
        self.signal = False
        self._above_since: float | None = None
        self._below_since: float | None = None
        self.history: deque[Level] = deque()

    @property
    def off_threshold_db(self) -> float:
        return self.threshold_db - self.hysteresis_db

    @property
    def latest(self) -> Level | None:
        return self.history[-1] if self.history else None

    def force_off(self) -> Event | None:
        """No readings any more (ffmpeg cannot open the input, UFO202 unplugged): treat as
        silence instead of keeping the last state forever."""
        self._above_since = self._below_since = None
        if not self.signal:
            return None
        self.signal = False
        return "off"

    def feed(self, level: Level) -> Event | None:
        self.history.append(level)
        while self.history and level.t - self.history[0].t > self.history_s:
            self.history.popleft()
        rms = level.rms_db if not math.isnan(level.rms_db) else -math.inf
        if not self.signal:
            if rms >= self.threshold_db:
                if self._above_since is None:
                    self._above_since = level.t
                if level.t - self._above_since >= self.on_after_s:
                    self.signal, self._above_since, self._below_since = True, None, None
                    return "on"
            else:
                self._above_since = None
        else:
            if rms < self.off_threshold_db:
                if self._below_since is None:
                    self._below_since = level.t
                if level.t - self._below_since >= self.off_after_s:
                    self.signal, self._above_since, self._below_since = False, None, None
                    return "off"
            else:
                self._below_since = None
        return None
