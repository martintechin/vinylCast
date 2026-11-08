"""Driver with a fake speaker session and fake service (no network, no hardware)."""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import wait_until

from vinylcast.cast import CastError, is_busy
from vinylcast.config import Config, load_config
from vinylcast.controller import State
from vinylcast.detector import Level
from vinylcast.driver import Driver
from vinylcast.metrics import Metrics
from vinylcast.state import StateStore


@dataclass
class FakeSession:
    name: str
    on_end: Callable[[str], None]
    log: list[str]
    busy: bool = False  # another app is running on the speaker
    found: bool = True
    latency: float | None = 1.0
    content_id: str | None = None  # what the speaker's media receiver last loaded

    async def connect(self) -> None:
        self.log.append(f"connect {self.name}")
        if not self.found:
            raise CastError("not found")

    async def is_busy(self, our_url: str) -> bool:
        # The real rule, applied to what this fake speaker is showing (a constant here hid
        # the codec-switch bug Martin found on the Pi).
        app = "CC32E753" if self.busy else ("CC1AD845" if self.content_id else None)
        return is_busy(app, self.content_id, our_url)

    async def play(self, url: str, content_type: str) -> float | None:
        self.log.append(f"play {self.name} {url} {content_type}")
        self.content_id = url
        return self.latency

    async def stop(self) -> None:
        self.log.append(f"stop {self.name}")

    async def set_volume(self, level: float) -> float:
        return level

    async def disconnect(self) -> None:
        self.log.append(f"disconnect {self.name}")


@dataclass
class FakeService:
    cfg: Config
    reconfigured: list[Config] = field(default_factory=list)

    async def reconfigure(self, cfg: Config) -> list[str]:
        changed = ["codec"] if cfg.codec != self.cfg.codec else []
        self.cfg = cfg
        self.reconfigured.append(cfg)
        return changed


class Rig:
    def __init__(self, tmp_path: Path, **session_kw: object) -> None:
        self.now = 0.0
        self.log: list[str] = []
        self.sessions: list[FakeSession] = []
        self.session_kw = session_kw
        self.store = StateStore(tmp_path / "state.json")
        cfg = load_config(overrides={"source": "tone", "advertise_ip": "10.0.0.2",
                                     "cast_target": "HESTON 120",
                                     "detect_threshold_db": -50, "detect_on_s": 1.0})  # fmt: skip
        self.service = FakeService(cfg)
        self.driver = Driver(
            self.service, self.store, self.store.load(),  # empty: target from cast_target
            Metrics(tmp_path / "m.jsonl"), self.factory, clock=lambda: self.now, tick_s=0.005,
        )  # fmt: skip

    def factory(self, name: str, on_end: Callable[[str], None]) -> FakeSession:
        s = FakeSession(name, on_end, self.log, **self.session_kw)  # type: ignore[arg-type]
        self.sessions.append(s)
        return s

    def levels(self, seconds: float, rms: float) -> None:
        end = self.now + seconds
        while self.now < end:
            self.driver.on_level(Level(self.now, rms, rms + 10))
            self.now = round(self.now + 0.1, 3)


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    r = Rig(tmp_path)
    task = asyncio.create_task(r.driver.run())
    yield r
    task.cancel()


def state(r: Rig) -> State:
    return r.driver.controller.state


async def test_needle_drop_plays_on_last_speaker(rig: Rig) -> None:
    rig.levels(2, -30)
    await wait_until(lambda: state(rig) == State.PLAYING)
    assert rig.log == ["connect HESTON 120",
                       "play HESTON 120 http://10.0.0.2:8800/stream.mp3 audio/mpeg"]  # fmt: skip
    assert rig.store.load().last_target == "HESTON 120"


async def test_silence_stops_after_timeout(rig: Rig) -> None:
    rig.levels(2, -30)
    await wait_until(lambda: state(rig) == State.PLAYING)
    rig.levels(2, -80)
    off_at = rig.now - 1.0  # detector reports "off" after 1 s below the off threshold
    rig.now = off_at + 179
    await asyncio.sleep(0.05)
    assert state(rig) == State.PLAYING
    rig.now = off_at + 181
    await wait_until(lambda: state(rig) == State.IDLE)
    assert rig.log[-1] == "stop HESTON 120"


async def test_speaker_reports_cancel_then_hold(rig: Rig) -> None:
    rig.levels(2, -30)
    await wait_until(lambda: state(rig) == State.PLAYING)
    rig.sessions[0].on_end("cancelled")
    await wait_until(lambda: state(rig) == State.IDLE)
    assert rig.driver.controller.hold == "manual"


async def test_receiver_error_recasts(rig: Rig) -> None:
    rig.levels(2, -30)
    await wait_until(lambda: state(rig) == State.PLAYING)
    rig.sessions[0].on_end("error")
    await wait_until(lambda: sum(e.startswith("play") for e in rig.log) == 2)
    await wait_until(lambda: state(rig) == State.PLAYING)


async def test_codec_change_while_playing_recasts_new_stream(rig: Rig, tmp_path: Path) -> None:
    rig.levels(2, -30)
    await wait_until(lambda: state(rig) == State.PLAYING)
    cfg = await rig.driver.apply_settings({"codec": "flac"})
    assert cfg.codec == "flac" and rig.service.cfg.codec == "flac"
    await wait_until(lambda: rig.log[-1].endswith("/stream.flac audio/flac"))
    await wait_until(lambda: state(rig) == State.PLAYING)
    assert rig.store.load().settings == {"codec": "flac"}


async def test_manual_switch_speaker(rig: Rig) -> None:
    rig.levels(2, -30)
    await wait_until(lambda: state(rig) == State.PLAYING)
    rig.driver.play("Tvättstugan")
    await wait_until(lambda: any(e.startswith("play Tvättstugan") for e in rig.log))
    assert "stop HESTON 120" in rig.log and "disconnect HESTON 120" in rig.log
    await wait_until(lambda: rig.store.load().last_target == "Tvättstugan")


async def test_busy_speaker_not_taken_over(tmp_path: Path) -> None:
    r = Rig(tmp_path, busy=True)
    task = asyncio.create_task(r.driver.run())
    try:
        r.levels(2, -30)
        await wait_until(lambda: r.driver.controller.hold == "busy")
        assert not any(e.startswith("play") for e in r.log)
        assert "busy" in r.driver.notifications[-1]["message"]
    finally:
        task.cancel()


async def test_speaker_not_found(tmp_path: Path) -> None:
    r = Rig(tmp_path, found=False)
    task = asyncio.create_task(r.driver.run())
    try:
        r.levels(2, -30)
        await wait_until(lambda: r.driver.controller.hold == "unavailable")
        assert r.driver.notifications[-1]["level"] == "error"
    finally:
        task.cancel()


async def test_no_playing_state_counts_as_failure(tmp_path: Path) -> None:
    r = Rig(tmp_path, latency=None)
    task = asyncio.create_task(r.driver.run())
    try:
        r.levels(2, -30)
        await wait_until(lambda: r.driver.controller.hold == "errors")
        assert state(r) == State.IDLE
    finally:
        task.cancel()


async def test_status_snapshot(rig: Rig) -> None:
    rig.levels(0.5, -42)
    st = rig.driver.status()
    assert st["target"] == "HESTON 120" and st["state"] == "idle"
    assert st["level"] == {"rms_db": -42.0, "peak_db": -32.0}
