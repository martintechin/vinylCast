"""R2 web API against a real Service (tone source) and real Driver; only the speaker is fake."""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import pytest
from conftest import requires_ffmpeg, wait_until
from test_driver import FakeSession

from vinylcast.cast import Target
from vinylcast.config import load_config
from vinylcast.driver import Driver
from vinylcast.service import Service
from vinylcast.state import StateStore
from vinylcast.web import WebApi

pytestmark = requires_ffmpeg

DEVICES = [
    Target("HESTON 120", "HESTON 120", "audio", "192.168.1.65", 8009, "a"),
    Target("Tvättstugan", "Google Nest Mini", "audio", "192.168.1.124", 8009, "b"),
    Target("Downstairs", "Google Cast Group", "group", "192.168.1.65", 32187, "c"),
]


@dataclass
class Api:
    base: str
    driver: Driver
    service: Service
    store: StateStore
    log: list[str]


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    cfg = load_config(overrides={"source": "tone", "cast_target": "HESTON 120",
                                 "advertise_ip": "10.0.0.2", "auto_cast": False,
                                 "metrics_path": str(tmp_path / "m.jsonl")})  # fmt: skip
    service = Service(cfg, sample_interval_s=0.5)
    store = StateStore(tmp_path / "state.json")
    log: list[str] = []

    def factory(name: str, on_end: Callable[[str], None]) -> FakeSession:
        return FakeSession(name, on_end, log)

    driver = Driver(service, store, store.load(), service.metrics, factory,
                    cli_overrides={"source": "tone", "advertise_ip": "10.0.0.2",
                                   "metrics_path": cfg.metrics_path})  # fmt: skip
    service.level_listeners.append(driver.on_level)
    WebApi(service, driver, lambda: DEVICES).install(service.server.app)
    port = await service.start(port=0)
    task = asyncio.create_task(driver.run())
    yield Api(f"http://127.0.0.1:{port}", driver, service, store, log)
    task.cancel()
    await service.stop()


async def get(url: str) -> tuple[int, object]:
    async with aiohttp.ClientSession() as s, s.get(url) as r:
        return r.status, await r.json()


async def post(url: str, data: object = None, raw: str | None = None) -> tuple[int, object]:
    async with aiohttp.ClientSession() as s:
        kw = (
            {"data": raw, "headers": {"Content-Type": "application/json"}}
            if raw
            else {"json": data}
        )
        async with s.post(url, **kw) as r:
            return r.status, await r.json()


async def test_status(api: Api) -> None:
    code, st = await get(f"{api.base}/api/status")
    assert code == 200 and isinstance(st, dict)
    assert st["state"] == "idle" and st["target"] == "HESTON 120" and st["codec"] == "mp3"
    assert st["stream_url"] == "http://10.0.0.2:8800/stream.mp3"
    assert {"encoder", "clients", "signal", "hold", "auto_cast", "notifications"} <= st.keys()


async def test_devices_marks_selected(api: Api) -> None:
    code, devs = await get(f"{api.base}/api/devices")
    assert code == 200 and isinstance(devs, list)
    assert [(d["name"], d["kind"], d["selected"]) for d in devs] == [
        ("HESTON 120", "device", True), ("Tvättstugan", "device", False),
        ("Downstairs", "group", False)]  # fmt: skip


async def test_play_volume_stop(api: Api) -> None:
    code, _ = await post(f"{api.base}/api/volume", {"level": 0.5})
    assert code == 409  # nothing playing yet
    code, _ = await post(f"{api.base}/api/play", {"target": "Tvättstugan"})
    assert code == 202
    await wait_until(lambda: api.driver.controller.state.value == "playing")
    assert api.log[-1] == "play Tvättstugan http://10.0.0.2:8800/stream.mp3 audio/mpeg"
    await wait_until(lambda: api.store.load().last_target == "Tvättstugan")
    assert await post(f"{api.base}/api/volume", {"level": 0.4}) == (200, {"level": 0.4})
    assert (await post(f"{api.base}/api/volume", {"level": 2}))[0] == 400
    code, st = await post(f"{api.base}/api/stop")
    assert code == 202
    await wait_until(lambda: api.driver.controller.state.value == "idle")
    assert api.driver.controller.hold == "manual" and api.log[-1] == "stop Tvättstugan"


async def test_settings_codec_switch_recasts(api: Api) -> None:
    await post(f"{api.base}/api/play", {})
    await wait_until(lambda: api.driver.controller.state.value == "playing")
    code, settings = await post(f"{api.base}/api/settings", {"codec": "flac"})
    assert code == 200 and isinstance(settings, dict) and settings["codec"] == "flac"
    await wait_until(lambda: api.log[-1].endswith("/stream.flac audio/flac"))
    await wait_until(lambda: api.driver.controller.state.value == "playing")
    assert api.store.load().settings == {"codec": "flac"}
    async with aiohttp.ClientSession() as s, s.head(f"{api.base}/stream.flac") as r:
        assert r.status == 200
    code, _ = await get(f"{api.base}/api/settings")
    assert code == 200


async def test_settings_rejects_bad_input(api: Api) -> None:
    code, err = await post(f"{api.base}/api/settings", {"http_port": 1})
    assert code == 400 and "not changeable" in err["error"]  # type: ignore[index]
    code, err = await post(f"{api.base}/api/settings", {"stop_after_silence_s": -5})
    assert code == 400
    code, err = await post(f"{api.base}/api/settings", raw="{not json")
    assert code == 400 and "invalid JSON" in err["error"]  # type: ignore[index]
    assert api.store.load().settings == {}  # nothing persisted


async def test_events_stream_status_and_levels(api: Api) -> None:
    got: dict[str, int] = {}
    async with aiohttp.ClientSession() as s, s.get(f"{api.base}/api/events") as r:
        assert r.headers["Content-Type"] == "text/event-stream"
        async with asyncio.timeout(5):
            while got.get("level", 0) < 3:
                line = (await r.content.readline()).decode()
                if line.startswith("event: "):
                    name = line[7:].strip()
                    got[name] = got.get(name, 0) + 1
    assert got["status"] >= 1 and got["level"] >= 3


async def test_diagnostics(api: Api) -> None:
    await post(f"{api.base}/api/stop")
    code, d = await get(f"{api.base}/api/diagnostics")
    assert code == 200 and isinstance(d, dict)
    assert {"health", "sample", "notifications", "events"} <= d.keys()
    assert not any(e.get("event") == "http_request" and e.get("method") == "GET"
                   for e in d["events"])  # fmt: skip


async def test_index_page_served(api: Api) -> None:
    async with aiohttp.ClientSession() as s, s.get(f"{api.base}/") as r:
        assert r.status == 200 and r.headers["Content-Type"].startswith("text/html")
        assert r.headers["Cache-Control"] == "no-cache"
        html = await r.text()
    assert "<title>VinylCast</title>" in html and 'new EventSource("/api/events")' in html
    assert "http://" not in html.split("<script>")[1] and "https://" not in html  # no externals


async def test_choose_speaker_without_playing(api: Api) -> None:
    code, st = await post(f"{api.base}/api/target", {"target": "Tvättstugan"})
    assert code == 200 and st["target"] == "Tvättstugan" and st["state"] == "idle"  # type: ignore[index]
    assert api.store.load().last_target == "Tvättstugan"
    assert not any(e.startswith("play") for e in api.log)
    assert (await post(f"{api.base}/api/target", {"target": ""}))[0] == 400
