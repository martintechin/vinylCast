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


async def test_no_startup_event_without_play(tmp_path: Path) -> None:
    """Regression (seen on Martin's Pi): `stop` connects to an already-playing target; that
    PLAYING state must not be logged as a startup with latency null."""
    path = tmp_path / "m.jsonl"
    mon = StatusMonitor(Metrics(path), "Tvättstugan", asyncio.get_running_loop())
    from_thread(mon.new_media_status, status("PLAYING"))
    assert events(path, "cast_startup") == []
    assert [e["player_state"] for e in events(path, "cast_state")] == ["PLAYING"]


def test_classify_idle() -> None:
    from vinylcast.cast import classify_idle

    assert classify_idle("CANCELLED") == "cancelled"
    assert classify_idle("INTERRUPTED") == "taken_over"
    assert classify_idle("ERROR") == "error"
    assert classify_idle("FINISHED") == "error"  # endless stream ended = connection problem
    assert classify_idle(None) == "error"


def test_classify_app_change() -> None:
    from vinylcast.cast import classify_app_change

    dmr, spotify, backdrop = "CC1AD845", "CC32E753", "E8C28D3C"
    assert classify_app_change(dmr, spotify) == "taken_over"
    assert classify_app_change(dmr, None) == "cancelled"
    assert classify_app_change(dmr, backdrop) == "cancelled"
    assert classify_app_change(None, dmr) is None
    assert classify_app_change(spotify, None) is None  # not ours


def test_is_busy() -> None:
    from vinylcast.cast import is_busy

    url = "http://192.168.1.102:8800/stream.mp3"
    assert not is_busy(None, None, url)
    assert not is_busy("E8C28D3C", None, url)  # idle screen
    assert not is_busy("CC1AD845", None, url)  # media receiver, nothing loaded
    assert not is_busy("CC1AD845", url, url)  # already ours
    assert is_busy("CC1AD845", "http://other/x.mp3", url)
    assert is_busy("CC32E753", None, url)  # another app
    # Regression (Pi): after a codec switch the speaker still shows our old MP3 URL.
    assert not is_busy("CC1AD845", url, "http://192.168.1.102:8800/stream.flac")
    assert is_busy("CC1AD845", "http://192.168.1.50:8800/stream.mp3", url)  # other server


async def test_monitor_reports_end_once(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    ends: list[str] = []
    mon = StatusMonitor(Metrics(path), "T", asyncio.get_running_loop(), ends.append)
    mon.play_sent()
    from_thread(mon.new_media_status, status("PLAYING"))
    from_thread(mon.new_media_status, status("IDLE", "CANCELLED"))
    from_thread(mon.new_connection_status, SimpleNamespace(status="LOST", address=None))
    await asyncio.sleep(0.05)
    assert ends == ["cancelled"]  # only the first end per play
    assert [e["reason"] for e in events(path, "cast_ended")] == ["cancelled"]


async def test_monitor_ignores_own_disconnect_and_pre_play_idle(tmp_path: Path) -> None:
    ends: list[str] = []
    mon = StatusMonitor(Metrics(tmp_path / "m.jsonl"), "T", asyncio.get_running_loop(), ends.append)
    mon.play_sent()
    from_thread(mon.new_media_status, status("IDLE"))  # before first PLAYING: not an end
    from_thread(mon.new_media_status, status("PLAYING"))
    mon.closing = True
    from_thread(mon.new_connection_status, SimpleNamespace(status="DISCONNECTED", address=None))
    await asyncio.sleep(0.05)
    assert ends == []


async def test_monitor_app_takeover(tmp_path: Path) -> None:
    ends: list[str] = []
    mon = StatusMonitor(Metrics(tmp_path / "m.jsonl"), "T", asyncio.get_running_loop(), ends.append)
    app = lambda a: SimpleNamespace(app_id=a, display_name=a)  # noqa: E731
    from_thread(mon.new_cast_status, app("CC1AD845"))
    mon.play_sent()
    from_thread(mon.new_media_status, status("PLAYING"))
    from_thread(mon.new_cast_status, app("CC32E753"))
    await asyncio.sleep(0.05)
    assert ends == ["taken_over"]


def test_session_volume_from_receiver_status() -> None:
    from vinylcast.cast import CastSession

    s = CastSession("T", Metrics(None))
    assert s.volume is None  # not connected
    s._cast = SimpleNamespace(status=None)  # type: ignore[assignment]
    assert s.volume is None  # connected, no status yet
    s._cast = SimpleNamespace(status=SimpleNamespace(volume_level=0.33333))  # type: ignore[assignment]
    assert s.volume == 0.333
