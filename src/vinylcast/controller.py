"""Auto-cast control: the one place that decides what plays where (revision 1).

Pure logic, no I/O. Every input carries `now` (monotonic seconds); every method returns the
actions a driver must execute (`Cast`, `Stop`, `Notify`). The driver reports back with
`cast_started` / `cast_failed` / `cast_ended` / `stopped`, and calls `tick` about once a
second for the silence timer. Rules (docs/PLAN-rev1.md, "Auto-cast rules"):

- Signal on while IDLE, auto-cast enabled, no hold, a speaker known -> Cast (needle drop).
- Silence for `stop_after_silence_s` while PLAYING (auto-cast enabled) -> Stop.
  Shorter silences (side flip, track gaps) keep playing.
- Manual stop (here, or the receiver reports CANCELLED / another app took over) -> hold:
  no auto-cast until the input has been silent for a full `stop_after_silence_s`.
- Speaker busy or not found -> Notify + hold (no retry loop on every track).
- Receiver error / connection lost while there is signal -> re-cast, at most `recast_max`
  times per `recast_window_s`; beyond that Notify + hold.
- Auto-cast disabled -> no automatic Cast and no automatic Stop; manual play/stop work.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class State(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    PLAYING = "playing"
    STOPPING = "stopping"


@dataclass(frozen=True)
class Cast:
    target: str
    reason: str  # "needle_drop" | "manual" | "recover" | "switch"


@dataclass(frozen=True)
class Stop:
    reason: str  # "silence" | "manual" | "switch"


@dataclass(frozen=True)
class Notify:
    level: Literal["info", "warning", "error"]
    message: str


Action = Cast | Stop | Notify
CastFailure = Literal["busy", "not_found", "error"]
CastEnd = Literal["cancelled", "taken_over", "error", "lost"]
TransitionHook = Callable[[State, State, str], None]


class Controller:
    def __init__(
        self,
        *,
        auto_cast: bool = True,
        stop_after_silence_s: float = 180.0,
        recast_max: int = 3,
        recast_window_s: float = 600.0,
        target: str | None = None,
        on_transition: TransitionHook | None = None,
    ) -> None:
        self.auto_cast = auto_cast
        self.stop_after_silence_s = stop_after_silence_s
        self.recast_max = recast_max
        self.recast_window_s = recast_window_s
        self.target = target
        self._on_transition = on_transition
        self.state = State.IDLE
        self.signal = False
        self.silence_since: float | None = None
        self.hold: str | None = None  # "manual" | "taken_over" | "busy" | "unavailable" | "errors"
        self._hold_since: float | None = None
        self._recasts: deque[float] = deque()

    # ---------------------------------------------------------------- helpers

    def _go(self, new: State, reason: str) -> None:
        old, self.state = self.state, new
        if old != new and self._on_transition is not None:
            self._on_transition(old, new, reason)

    def _set_hold(self, now: float, reason: str) -> None:
        self.hold, self._hold_since = reason, now

    def _silent_for(self, now: float) -> float:
        if self.signal or self.silence_since is None:
            return 0.0
        return now - self.silence_since

    # ---------------------------------------------------------------- signal

    def signal_on(self, now: float) -> list[Action]:
        self.signal, self.silence_since = True, None
        if self.state != State.IDLE or not self.auto_cast or self.hold is not None:
            return []
        if self.target is None:
            return [Notify("warning", "Music detected, but no speaker chosen yet")]
        self._go(State.STARTING, "needle_drop")
        return [Cast(self.target, "needle_drop")]

    def signal_off(self, now: float) -> list[Action]:
        self.signal, self.silence_since = False, now
        return []

    def tick(self, now: float) -> list[Action]:
        actions: list[Action] = []
        if self.hold is not None and not self.signal and self.silence_since is not None:
            # A full silence period after the hold started ends it (next record).
            assert self._hold_since is not None
            since = max(self.silence_since, self._hold_since)
            if now - since >= self.stop_after_silence_s:
                self.hold = self._hold_since = None
        if (
            self.state == State.PLAYING
            and self.auto_cast
            and self._silent_for(now) >= self.stop_after_silence_s
        ):
            self._go(State.STOPPING, "silence")
            actions.append(Stop("silence"))
        return actions

    # ---------------------------------------------------------------- commands

    def set_target(self, target: str) -> None:
        self.target = target

    def play(self, now: float, target: str | None = None) -> list[Action]:
        if target is not None:
            switching = self.state in (State.STARTING, State.PLAYING) and target != self.target
            self.target = target
        else:
            switching = False
        if self.target is None:
            return [Notify("warning", "Choose a speaker first")]
        self.hold = self._hold_since = None
        if self.state in (State.STARTING, State.PLAYING) and not switching:
            return []
        actions: list[Action] = [Stop("switch")] if switching else []
        self._go(State.STARTING, "switch" if switching else "manual")
        return [*actions, Cast(self.target, "switch" if switching else "manual")]

    def restart(self, now: float) -> list[Action]:
        """Re-cast the current speaker (e.g. after a codec change changed the stream URL)."""
        if self.state not in (State.STARTING, State.PLAYING) or self.target is None:
            return []
        self._go(State.STARTING, "restart")
        return [Cast(self.target, "restart")]

    def stop(self, now: float) -> list[Action]:
        self._set_hold(now, "manual")
        if self.state in (State.STARTING, State.PLAYING):
            self._go(State.STOPPING, "manual")
            return [Stop("manual")]
        return []

    # ---------------------------------------------------------------- cast feedback

    def cast_started(self, now: float) -> list[Action]:
        if self.state == State.STARTING:
            self._go(State.PLAYING, "receiver playing")
            if not self.signal:
                # e.g. a manual play before the needle drops: the silence timer starts now.
                self.silence_since = now
        return []

    def cast_failed(self, now: float, reason: CastFailure) -> list[Action]:
        if self.state != State.STARTING:
            return []
        self._go(State.IDLE, f"cast failed: {reason}")
        if reason == "busy":
            self._set_hold(now, "busy")
            return [Notify("warning", f"{self.target} is busy with another app; not taking over")]
        if reason == "not_found":
            self._set_hold(now, "unavailable")
            return [Notify("error", f"{self.target} not found on the network")]
        self._set_hold(now, "errors")
        return [Notify("error", f"Could not start playback on {self.target}")]

    def cast_ended(self, now: float, reason: CastEnd) -> list[Action]:
        if self.state == State.STOPPING:
            self._go(State.IDLE, "stopped")
            return []
        if self.state not in (State.STARTING, State.PLAYING):
            return []
        if reason in ("cancelled", "taken_over"):
            self._go(State.IDLE, f"ended: {reason}")
            self._set_hold(now, "manual" if reason == "cancelled" else "taken_over")
            what = "Stopped on the speaker" if reason == "cancelled" else "Another app took over"
            return [Notify("info", f"{what}; waiting for the next record")]
        # error / lost: recover while the record is still playing, within the budget.
        while self._recasts and now - self._recasts[0] > self.recast_window_s:
            self._recasts.popleft()
        if self.signal and self.target and len(self._recasts) < self.recast_max:
            self._recasts.append(now)
            self._go(State.STARTING, f"recover after {reason}")
            return [Cast(self.target, "recover")]
        self._go(State.IDLE, f"ended: {reason}")
        self._set_hold(now, "errors")
        return [Notify("error", f"Playback on {self.target} failed ({reason}); gave up")]

    def stopped(self, now: float) -> list[Action]:
        if self.state == State.STOPPING:
            self._go(State.IDLE, "stopped")
        return []
