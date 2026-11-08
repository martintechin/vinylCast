"""Cast discovery, play, stop and status logging via pychromecast.

pychromecast (verified against 14.1.1) is synchronous and runs its own socket and
zeroconf threads. Blocking calls are run with `asyncio.to_thread`; its listener
callbacks arrive on its threads and only touch thread-safe state (Metrics has a lock)
or hand over to the event loop with `call_soon_threadsafe`.

cast.py only hands the receiver a URL. From then on the receiver pulls the stream from
server.py like any other HTTP client.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pychromecast
from pychromecast.config import APP_BACKDROP, APP_MEDIA_RECEIVER
from pychromecast.const import CAST_TYPE_GROUP
from pychromecast.controllers.media import MediaStatus, MediaStatusListener
from pychromecast.controllers.receiver import (
    CastStatus,
    CastStatusListener,
    LaunchErrorListener,
    LaunchFailure,
)
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.socket_client import ConnectionStatus, ConnectionStatusListener

from vinylcast.metrics import Metrics

DISCOVERY_TIMEOUT_S = 5.0
CONNECT_TIMEOUT_S = 15.0
PLAYING_TIMEOUT_S = 20.0
TITLE = "VinylCast"


class CastError(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    name: str
    model: str
    cast_type: str  # pychromecast: "cast", "audio" or "group"
    host: str
    port: int
    uuid: str

    @property
    def kind(self) -> str:
        return "group" if self.cast_type == CAST_TYPE_GROUP else "device"


def discover(timeout_s: float = DISCOVERY_TIMEOUT_S) -> list[Target]:
    """Browse mDNS for `timeout_s` and return every Cast device and group seen (blocking)."""
    browser = CastBrowser(SimpleCastListener(), pychromecast.discovery.create_zeroconf())
    browser.start_discovery()
    try:
        time.sleep(timeout_s)
        infos = list(browser.devices.values())
    finally:
        browser.stop_discovery()
    return sorted(
        (
            Target(
                name=i.friendly_name or "?",
                model=i.model_name or "?",
                cast_type=i.cast_type or "?",
                host=i.host,
                port=i.port,
                uuid=str(i.uuid),
            )
            for i in infos
        ),
        key=lambda t: (t.kind, t.name.lower()),
    )


# Why playback ended, in the controller's terms (controller.CastEnd).
EndReason = str  # "cancelled" | "taken_over" | "error" | "lost"
IDLE_APPS = (None, APP_BACKDROP)  # pychromecast treats these as "nothing running"


def classify_idle(idle_reason: str | None) -> EndReason:
    """Media went IDLE after playing. CANCELLED = stopped by a user (Google Home app, voice,
    our own stop); INTERRUPTED = someone loaded other media; FINISHED on an endless stream
    means the connection to our server ended, so it is treated like ERROR (recoverable)."""
    if idle_reason == "CANCELLED":
        return "cancelled"
    if idle_reason == "INTERRUPTED":
        return "taken_over"
    return "error"


def classify_app_change(old_app: str | None, new_app: str | None) -> EndReason | None:
    """Receiver app changed while we were playing in the Default Media Receiver."""
    if old_app != APP_MEDIA_RECEIVER or new_app == APP_MEDIA_RECEIVER:
        return None
    return "cancelled" if new_app in IDLE_APPS else "taken_over"


def is_busy(app_id: str | None, content_id: str | None, our_url: str) -> bool:
    """Would casting now interrupt someone? Free when nothing runs, or when the Default Media
    Receiver is idle or already playing our own stream."""
    if app_id in IDLE_APPS:
        return False
    if app_id == APP_MEDIA_RECEIVER:
        return content_id not in (None, our_url)
    return True


class StatusMonitor(
    MediaStatusListener, ConnectionStatusListener, LaunchErrorListener, CastStatusListener
):
    """Logs every player-state change, connection change and launch/load error, and
    measures startup latency (play_media sent -> first PLAYING).

    Callbacks run on pychromecast threads; `playing` is an asyncio.Event set via the loop.
    """

    def __init__(
        self,
        metrics: Metrics,
        target: str,
        loop: asyncio.AbstractEventLoop,
        on_end: Callable[[EndReason], None] | None = None,
    ) -> None:
        self._metrics = metrics
        self._target = target
        self._loop = loop
        self._on_end = on_end
        self._lock = threading.Lock()
        self._app_id: str | None = None
        self._ended = False
        self.closing = False  # set before an intended disconnect: not a "lost" connection
        self._last: tuple[str, str | None] | None = None
        self._play_sent: float | None = None
        self.seen_playing = False
        self.startup_latency_s: float | None = None
        self.playing = asyncio.Event()

    def play_sent(self) -> None:
        with self._lock:
            self._play_sent = time.monotonic()
            self.seen_playing = False
            self._ended = False
            self._last = None
        self._loop.call_soon_threadsafe(self.playing.clear)

    def new_media_status(self, status: MediaStatus) -> None:
        state = (status.player_state, status.idle_reason)
        with self._lock:
            if state == self._last:
                return
            self._last = state
            first_playing = status.player_state == "PLAYING" and not self.seen_playing
            if first_playing:
                self.seen_playing = True
            # Startup is only measured by the session that sent play_media; a session that
            # connects to an already-playing target (`stop`) just logs the state.
            startup = first_playing and self._play_sent is not None
            if startup:
                assert self._play_sent is not None
                self.startup_latency_s = round(time.monotonic() - self._play_sent, 3)
            after_playing = self.seen_playing and not first_playing
        self._metrics.event(
            "cast_state",
            target=self._target,
            player_state=status.player_state,
            idle_reason=status.idle_reason,
            stream_type=status.stream_type,
            content_id=status.content_id,
            after_first_playing=after_playing,
        )
        if startup:
            self._metrics.event(
                "cast_startup", target=self._target, latency_s=self.startup_latency_s
            )
        if first_playing:
            self._loop.call_soon_threadsafe(self.playing.set)
        if status.player_state == "IDLE" and after_playing:
            self._end(classify_idle(status.idle_reason))

    def _end(self, reason: EndReason) -> None:
        """Report the end of our playback once per play_media, on the event loop."""
        with self._lock:
            if self._ended or not self.seen_playing:
                return
            self._ended = True
        self._metrics.event("cast_ended", target=self._target, reason=reason)
        if self._on_end is not None:
            self._loop.call_soon_threadsafe(self._on_end, reason)

    def new_cast_status(self, status: CastStatus) -> None:
        with self._lock:
            old, self._app_id = self._app_id, status.app_id
        if old != status.app_id:
            self._metrics.event(
                "cast_app", target=self._target, app_id=status.app_id, app=status.display_name
            )
            reason = classify_app_change(old, status.app_id)
            if reason is not None:
                self._end(reason)

    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        self._metrics.event(
            "cast_load_failed", target=self._target, item=queue_item_id, error_code=error_code
        )

    def new_connection_status(self, status: ConnectionStatus) -> None:
        address = f"{status.address.address}:{status.address.port}" if status.address else None
        self._metrics.event(
            "cast_connection", target=self._target, status=status.status, address=address
        )
        if status.status in ("LOST", "FAILED", "DISCONNECTED") and not self.closing:
            self._end("lost")

    def new_launch_error(self, status: LaunchFailure) -> None:
        self._metrics.event(
            "cast_launch_error", target=self._target, reason=status.reason, app_id=status.app_id
        )


class CastSession:
    """One connection to a named device or group."""

    def __init__(
        self, name: str, metrics: Metrics, on_end: Callable[[EndReason], None] | None = None
    ) -> None:
        self.name = name
        self._metrics = metrics
        self._on_end = on_end
        self._cast: pychromecast.Chromecast | None = None
        self._browser: CastBrowser | None = None
        self.monitor: StatusMonitor | None = None

    def _connect_blocking(self) -> pychromecast.Chromecast:
        casts, browser = pychromecast.get_listed_chromecasts(
            friendly_names=[self.name], discovery_timeout=DISCOVERY_TIMEOUT_S * 2
        )
        self._browser = browser
        if not casts:
            seen = sorted(i.friendly_name or "?" for i in browser.devices.values())
            browser.stop_discovery()
            self._browser = None
            raise CastError(
                f"Cast target {self.name!r} not found. Seen: {', '.join(seen) or 'none'}"
            )
        cast = casts[0]
        cast.wait(timeout=CONNECT_TIMEOUT_S)
        return cast

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        self.monitor = StatusMonitor(self._metrics, self.name, loop, self._on_end)
        started = time.monotonic()
        self._cast = await asyncio.to_thread(self._connect_blocking)
        info = self._cast.cast_info
        self._cast.media_controller.register_status_listener(self.monitor)
        self._cast.register_connection_listener(self.monitor)
        self._cast.register_launch_error_listener(self.monitor)
        self._cast.register_status_listener(self.monitor)  # receiver: running app
        if self._cast.status is not None:
            self.monitor.new_cast_status(self._cast.status)
        self._metrics.event(
            "cast_connected",
            target=self.name,
            model=info.model_name,
            cast_type=info.cast_type,
            host=info.host,
            port=info.port,
            connect_s=round(time.monotonic() - started, 2),
        )

    async def play(self, url: str, content_type: str) -> float | None:
        """Send play_media and wait for the first PLAYING. Returns startup latency in seconds,
        or None if PLAYING did not arrive within PLAYING_TIMEOUT_S."""
        assert self._cast is not None and self.monitor is not None
        self.monitor.play_sent()
        self._metrics.event("cast_play_media", target=self.name, url=url, content_type=content_type)
        await asyncio.to_thread(
            self._cast.media_controller.play_media,
            url,
            content_type,
            title=TITLE,
            stream_type="LIVE",
        )
        try:
            await asyncio.wait_for(self.monitor.playing.wait(), PLAYING_TIMEOUT_S)
        except TimeoutError:
            self._metrics.event(
                "cast_startup_timeout", target=self.name, timeout_s=PLAYING_TIMEOUT_S
            )
            return None
        return self.monitor.startup_latency_s

    def _busy_blocking(self, our_url: str) -> bool:
        assert self._cast is not None
        app_id = self._cast.app_id
        content_id = None
        if app_id == APP_MEDIA_RECEIVER:
            mc = self._cast.media_controller
            mc.update_status()
            mc.block_until_active(timeout=2)
            content_id = mc.status.content_id
        return is_busy(app_id, content_id, our_url)

    async def is_busy(self, our_url: str) -> bool:
        """True if another app (or someone else's media) is playing on the target."""
        busy = await asyncio.to_thread(self._busy_blocking, our_url)
        if busy:
            assert self._cast is not None
            self._metrics.event("cast_busy", target=self.name, app_id=self._cast.app_id,
                                app=self._cast.app_display_name)  # fmt: skip
        return busy

    async def set_volume(self, level: float) -> float:
        """Speaker volume 0..1; returns what the receiver reports."""
        assert self._cast is not None
        return float(await asyncio.to_thread(self._cast.set_volume, level))

    def _stop_blocking(self) -> None:
        assert self._cast is not None
        mc = self._cast.media_controller
        mc.update_status()
        mc.block_until_active(timeout=3)
        if mc.status.media_session_id is not None:
            with contextlib.suppress(Exception):
                mc.stop()
        self._cast.quit_app()

    async def stop(self) -> None:
        """Stop media and quit the receiver app."""
        await asyncio.to_thread(self._stop_blocking)
        self._metrics.event("cast_stop", target=self.name)

    def _disconnect_blocking(self) -> None:
        if self._cast is not None:
            with contextlib.suppress(Exception):
                self._cast.disconnect(timeout=5)
        if self._browser is not None:
            with contextlib.suppress(Exception):
                self._browser.stop_discovery()

    async def disconnect(self) -> None:
        if self.monitor is not None:
            self.monitor.closing = True  # our own disconnect is not a lost connection
        await asyncio.to_thread(self._disconnect_blocking)
        self._cast = None
        self._browser = None


def target_dict(t: Target) -> dict[str, Any]:
    return {"name": t.name, "kind": t.kind, "cast_type": t.cast_type, "model": t.model,
            "host": t.host, "port": t.port}  # fmt: skip
