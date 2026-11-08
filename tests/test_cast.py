"""cast.py logic that needs no Cast device. Real-device checks are run by Martin (M3)."""

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import events

from vinylcast import cli
from vinylcast.cast import StatusMonitor, Target
from vinylcast.metrics import Metrics


def status(state: str, idle: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(player_state=state, idle_reason=idle, stream_type="LIVE", content_id="u")


def from_thread(fn, *args) -> None:  # type: ignore[no-untyped-def]
    t = threading.Thread(target=fn, args=args)  # pychromecast calls back from its threads
    t.start()
    t.join()


async def test_state_changes_logged_once_and_latency_measured(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    mon = StatusMonitor(Metrics(path), "Kitchen", asyncio.get_running_loop())
    mon.play_sent()
    for s in ("IDLE", "BUFFERING", "BUFFERING", "PLAYING", "PLAYING", "BUFFERING", "PLAYING"):
        from_thread(mon.new_media_status, status(s))
    from_thread(mon.new_media_status, status("IDLE", "ERROR"))
    await asyncio.wait_for(mon.playing.wait(), 1)

    states = [(e["player_state"], e["idle_reason"], e["after_first_playing"])
              for e in events(path, "cast_state")]  # fmt: skip
    assert states == [
        ("IDLE", None, False),
        ("BUFFERING", None, False),
        ("PLAYING", None, False),
        ("BUFFERING", None, True),  # a BUFFERING after the first PLAYING: soak-test failure signal
        ("PLAYING", None, True),
        ("IDLE", "ERROR", True),
    ]
    (startup,) = events(path, "cast_startup")
    assert startup["latency_s"] is not None and 0 <= startup["latency_s"] < 1
    assert mon.startup_latency_s == startup["latency_s"]


async def test_connection_and_errors_logged(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    mon = StatusMonitor(Metrics(path), "Group", asyncio.get_running_loop())
    addr = SimpleNamespace(address="192.168.1.50", port=32187)
    from_thread(mon.new_connection_status, SimpleNamespace(status="LOST", address=addr))
    from_thread(mon.load_media_failed, 1, 104)
    from_thread(mon.new_launch_error, SimpleNamespace(reason="NOT_FOUND", app_id="CC1AD845"))
    assert events(path, "cast_connection")[0]["address"] == "192.168.1.50:32187"
    assert events(path, "cast_load_failed")[0]["error_code"] == 104
    assert events(path, "cast_launch_error")[0]["reason"] == "NOT_FOUND"


def test_target_kind() -> None:
    def t(cast_type: str) -> Target:
        return Target("x", "m", cast_type, "1.2.3.4", 8009, "u")

    assert [t(c).kind for c in ("cast", "audio", "group")] == ["device", "device", "group"]


def test_devices_table(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    targets = [
        Target("Kitchen speaker", "Google Nest Mini", "audio", "192.168.1.40", 8009, "a"),
        Target("Downstairs", "Google Cast Group", "group", "192.168.1.40", 32187, "b"),
    ]
    import vinylcast.cast

    monkeypatch.setattr(vinylcast.cast, "discover", lambda: targets)
    assert cli.main(["devices"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split()[:2] == ["NAME", "KIND"]
    assert "group" in out[2] and "Downstairs" in out[2]
    assert "device" in out[1] and "192.168.1.40:8009" in out[1]


def test_devices_none_found(monkeypatch: pytest.MonkeyPatch) -> None:
    import vinylcast.cast

    monkeypatch.setattr(vinylcast.cast, "discover", lambda: [])
    assert cli.main(["devices"]) == 1


@pytest.mark.parametrize("command", ["cast", "stop", "run"])
def test_target_required(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([command, "--source", "tone"]) == 2
    assert "--to" in capsys.readouterr().err


def test_cast_needs_running_server(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["cast", "--to", "X", "--http-port", "1", "--metrics-path", str(tmp_path / "m")])
    assert rc == 1 and "no running server" in capsys.readouterr().err
