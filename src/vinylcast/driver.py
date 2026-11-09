"""Revision 1 driver: connects detector -> controller -> Cast, plus persistent state.

The controller decides (pure logic); the driver executes its actions one at a time on the
event loop and feeds the results back: level readings -> detector -> signal_on/off,
Cast feedback -> cast_started / cast_failed / cast_ended / stopped, `tick` every second.
Cast operations are serialized through one worker so they never overlap.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Protocol

from vinylcast.cast import CastError
from vinylcast.config import Config, load_config, validate_ui_settings
from vinylcast.controller import Action, Cast, Controller, Notify, Stop
from vinylcast.detector import Level, SignalDetector
from vinylcast.metrics import Metrics
from vinylcast.server import CONTENT_TYPES, stream_url
from vinylcast.state import PersistentState, StateStore

STALE_INPUT_S = 3.0  # no level readings for this long = silence (input gone)


class SessionPort(Protocol):
    name: str

    async def connect(self) -> None: ...
    async def is_busy(self, our_url: str) -> bool: ...
    async def play(self, url: str, content_type: str) -> float | None: ...
    async def stop(self) -> None: ...
    async def set_volume(self, level: float) -> float: ...
    @property
    def volume(self) -> float | None: ...
    async def disconnect(self) -> None: ...


class ServicePort(Protocol):
    cfg: Config

    async def reconfigure(self, cfg: Config) -> list[str]: ...


SessionFactory = Callable[[str, Callable[[str], None]], SessionPort]


class Driver:
    def __init__(
        self,
        service: ServicePort,
        store: StateStore,
        state: PersistentState,
        metrics: Metrics,
        session_factory: SessionFactory,
        *,
        toml_path: Any = None,
        cli_overrides: dict[str, Any] | None = None,
        url: Callable[[Config], str] = stream_url,
        clock: Callable[[], float] = time.monotonic,
        tick_s: float = 1.0,
    ) -> None:
        cfg = service.cfg
        self.service = service
        self.store = store
        self.state = state
        self.metrics = metrics
        self._factory = session_factory
        self._toml_path = toml_path
        self._cli_overrides = cli_overrides or {}
        self._url = url
        self._clock = clock
        self._tick_s = tick_s
        self.detector = SignalDetector(cfg.detect_threshold_db, on_after_s=cfg.detect_on_s)
        self.controller = Controller(
            auto_cast=cfg.auto_cast,
            stop_after_silence_s=cfg.stop_after_silence_s,
            recast_max=cfg.recast_max,
            target=state.last_target or cfg.cast_target,
            on_transition=lambda a, b, why: metrics.event(
                "ctl_state", old=a.value, new=b.value, reason=why
            ),
        )
        self.session: SessionPort | None = None
        self.notifications: deque[dict[str, Any]] = deque(maxlen=20)
        self._queue: asyncio.Queue[Action] = asyncio.Queue()
        self._casting = False  # ends reported while we (re)cast are our own doing
        # Every connection gets an id. End reports count only from the active connection and
        # only if we did not stop it ourselves: a speaker confirms OUR stop with IDLE/CANCELLED,
        # which must not look like a user's stop (bug on Martin's phone: speaker switch showed
        # "Stopped on the speaker" while the new speaker played).
        self._sids = itertools.count(1)
        self._active_sid: int | None = None
        self._stopped_sids: set[int] = set()

    # ------------------------------------------------------------- inputs

    def on_level(self, level: Level) -> None:
        event = self.detector.feed(level)
        now = self._clock()
        if event == "on":
            self.metrics.event("signal", on=True, rms_db=round(level.rms_db, 1))
            self._apply(self.controller.signal_on(now))
        elif event == "off":
            self.metrics.event("signal", on=False, rms_db=round(level.rms_db, 1))
            self._apply(self.controller.signal_off(now))

    def _on_end(self, sid: int, reason: str) -> None:
        if self._casting or sid != self._active_sid or sid in self._stopped_sids:
            if self._casting:
                why = "own (re)cast"
            elif sid != self._active_sid:
                why = "replaced connection"
            else:
                why = "our stop"
            self.metrics.event("cast_end_ignored", session=sid, reason=reason, why=why)
            return
        self._apply(self.controller.cast_ended(self._clock(), reason))  # type: ignore[arg-type]

    def _apply(self, actions: list[Action]) -> None:
        for action in actions:
            self._queue.put_nowait(action)

    # ------------------------------------------------------------- commands (UI / CLI)

    def set_target(self, target: str) -> None:
        """Choose the speaker for the next needle drop without starting playback."""
        self.controller.set_target(target)
        self.state.last_target = target
        self._save()
        self.metrics.event("target_set", target=target)

    def play(self, target: str | None = None) -> None:
        self._apply(self.controller.play(self._clock(), target))

    def stop(self) -> None:
        self._apply(self.controller.stop(self._clock()))

    async def set_volume(self, level: float) -> float | None:
        if self.session is None:
            return None
        return await self.session.set_volume(level)

    async def apply_settings(self, settings: dict[str, Any]) -> Config:
        """Validate, persist and apply UI settings. A stream change re-casts if playing."""
        merged = validate_ui_settings({**self.state.settings, **settings})
        cfg = load_config(self._toml_path, self._cli_overrides, merged)
        self.state.settings = merged
        self._save()
        changed = await self.service.reconfigure(cfg)
        c = self.controller
        c.auto_cast, c.stop_after_silence_s = cfg.auto_cast, cfg.stop_after_silence_s
        c.recast_max = cfg.recast_max
        self.detector.threshold_db, self.detector.on_after_s = (
            cfg.detect_threshold_db, cfg.detect_on_s)  # fmt: skip
        self.metrics.event("settings_applied", settings=settings, stream_changed=changed)
        if changed:
            self._apply(c.restart(self._clock()))
        return cfg

    # ------------------------------------------------------------- execution

    async def run(self) -> None:
        ticker = asyncio.create_task(self._ticker())
        try:
            while True:
                action = await self._queue.get()
                try:
                    await self._execute(action)
                except Exception as e:  # never let one failed action stop the driver
                    self.metrics.event("driver_error", action=repr(action), error=repr(e))
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self._tick_s)
            now = self._clock()
            latest = self.detector.latest
            if self.detector.signal and (latest is None or now - latest.t > STALE_INPUT_S):
                if self.detector.force_off() == "off":
                    age = None if latest is None else round(now - latest.t, 1)
                    self.metrics.event("signal_stale", seconds_without_readings=age)
                    self._apply(self.controller.signal_off(now))
            self._apply(self.controller.tick(now))

    async def _execute(self, action: Action) -> None:
        if isinstance(action, Notify):
            self.notifications.append({"ts": time.time(), "level": action.level,
                                       "message": action.message})  # fmt: skip
            self.metrics.event("notify", level=action.level, message=action.message)
        elif isinstance(action, Stop):
            await self._stop_session()
            self._apply(self.controller.stopped(self._clock()))
        elif isinstance(action, Cast):
            await self._cast(action)

    async def _stop_session(self) -> None:
        if self.session is not None:
            if self._active_sid is not None:
                self._stopped_sids.add(self._active_sid)
            with contextlib.suppress(Exception):
                await self.session.stop()

    async def _cast(self, action: Cast) -> None:
        cfg = self.service.cfg
        url = self._url(cfg)
        self._casting = True
        try:
            # A recovery always starts from a fresh connection: after a speaker power cycle or a
            # Wi-Fi outage the old one may be dead or half-open.
            if self.session is not None and (
                self.session.name != action.target or action.reason == "recover"
            ):
                await self._drop_session()
            if self.session is None:
                sid = next(self._sids)
                session = self._factory(action.target, lambda why, sid=sid: self._on_end(sid, why))
                try:
                    await session.connect()
                except CastError:
                    with contextlib.suppress(Exception):
                        await session.disconnect()
                    self._apply(self.controller.cast_failed(self._clock(), "not_found"))
                    return
                self.session, self._active_sid = session, sid
            if await self.session.is_busy(url):
                self._apply(self.controller.cast_failed(self._clock(), "busy"))
                return
            if self._active_sid is not None:
                self._stopped_sids.discard(self._active_sid)  # a new playback: its ends count
            latency = await self.session.play(url, CONTENT_TYPES[cfg.codec])
        except Exception as e:
            self.metrics.event("driver_error", action=repr(action), error=repr(e))
            await self._drop_session()
            self._apply(self.controller.cast_failed(self._clock(), "error"))
            return
        finally:
            self._casting = False
        if latency is None:
            await self._drop_session()  # the next attempt reconnects from scratch
            self._apply(self.controller.cast_failed(self._clock(), "error"))
            return
        self._apply(self.controller.cast_started(self._clock()))
        if self.state.last_target != action.target:
            self.state.last_target = action.target
            self._save()

    async def _drop_session(self) -> None:
        if self.session is not None:
            with contextlib.suppress(Exception):
                await self.session.disconnect()
        self.session, self._active_sid = None, None

    def _save(self) -> None:
        self.state.last_target = self.controller.target or self.state.last_target
        self.store.save(self.state)

    async def shutdown(self) -> None:
        """Stop playback (if any) and disconnect; used when the daemon exits."""
        if self.controller.state.value in ("starting", "playing"):
            await self._stop_session()
        if self.session is not None:
            await self.session.disconnect()
            self.session = None

    def status(self) -> dict[str, Any]:
        latest = self.detector.latest
        return {
            "state": self.controller.state.value,
            "target": self.controller.target,
            "hold": self.controller.hold,
            "signal": self.controller.signal,
            "auto_cast": self.controller.auto_cast,
            "recovering": self.controller.recovering,
            # Volume of the connected speaker (None without a connection); after a speaker
            # switch this is the new speaker's own value (Martin: the slider must follow it).
            "volume": self.session.volume if self.session is not None else None,
            "level": None if latest is None else
                {"rms_db": round(latest.rms_db, 1), "peak_db": round(latest.peak_db, 1)},
            "notifications": list(self.notifications)[-5:],
        }  # fmt: skip
