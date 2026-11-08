import json
from pathlib import Path

from vinylcast.soak import render, split_runs, summarize

# Martin's M4 run on the Pi (m4.jsonl, second run), verbatim apart from the config, which is
# shortened. It includes the pre-fix shutdown artefacts (encoder_exit 255 + encoder_restart
# after the stop): they come after the stop marker and must not count.
PI_RUN = """
{"ts":1794144792.888,"kind":"event","event":"service_start","config":{"source":"alsa","codec":"mp3","cast_target":"Tvättstugan","sample_rate":44100,"mp3_bitrate":"320k"},"mode":"run"}
{"ts":1794144792.907,"kind":"event","event":"encoder_start","pid":7391,"attempt":0}
{"ts":1794144793.2,"kind":"event","event":"cast_state","target":"Tvättstugan","player_state":"UNKNOWN","idle_reason":null,"after_first_playing":false}
{"ts":1794144793.209,"kind":"event","event":"cast_state","target":"Tvättstugan","player_state":"IDLE","idle_reason":null,"after_first_playing":false}
{"ts":1794144793.219,"kind":"event","event":"client_connect","client":1,"remote":"192.168.1.124","clients":1}
{"ts":1794144793.592,"kind":"event","event":"ffmpeg_warning","pid":7391,"line":"Guessed Channel Layout: stereo"}
{"ts":1794144795.666,"kind":"event","event":"cast_state","target":"Tvättstugan","player_state":"PLAYING","idle_reason":null,"after_first_playing":false}
{"ts":1794144795.667,"kind":"event","event":"cast_startup","target":"Tvättstugan","latency_s":2.488}
{"ts":1794144800.0,"kind":"sample","python":{"pid":1,"cpu_pct":4.0,"rss_mb":40.1},"ffmpeg":{"pid":7391,"cpu_pct":null,"rss_mb":20.0},"clients":1,"bytes_out":1000}
{"ts":1794144805.0,"kind":"sample","python":{"pid":1,"cpu_pct":6.0,"rss_mb":40.3},"ffmpeg":{"pid":7391,"cpu_pct":30.0,"rss_mb":20.2},"clients":1,"bytes_out":2000}
{"ts":1794144810.0,"kind":"health","throttled":"0x0","net_iface":"wlan0","wifi":{"connected":true,"freq_mhz":2462.0,"signal_dbm":-73,"tx_mbit":72.2,"rx_mbit":58.5,"power_save":false}}
{"ts":1794144812.0,"kind":"event","event":"input_peak","t":10.0,"peak_db":-10.3,"channels":{"1":-11.0,"2":-10.3},"clip_risk":false}
{"ts":1794144885.502,"kind":"event","event":"cast_state","target":"Tvättstugan","player_state":"IDLE","idle_reason":"CANCELLED","after_first_playing":true}
{"ts":1794144885.512,"kind":"event","event":"client_disconnect","client":1,"reason":"client_gone","bytes_sent":3672816,"connected_s":92.29,"clients":0}
{"ts":1794144885.662,"kind":"event","event":"encoder_exit","returncode":255,"ran_s":92.77}
{"ts":1794144885.662,"kind":"event","event":"encoder_restart","delay_s":1,"restarts":1}
{"ts":1794144886.0,"kind":"event","event":"service_stop"}
"""


def records(text: str) -> list[dict]:
    return [json.loads(line) for line in text.strip().splitlines()]


def test_pi_run_summary() -> None:
    (run,) = split_runs(records(PI_RUN))
    s = summarize(run, min_minutes=1.0)
    assert s.startup_latency_s == 2.488
    assert round(s.playing_s, 1) == round(1794144885.502 - 1794144795.666, 1)
    assert s.counts["encoder_restarts"] == 0  # after the stop marker
    assert s.counts["client_drops"] == 0  # receiver left after CANCELLED
    assert s.counts["ffmpeg_warnings"] == 1
    assert s.cpu["python"] == {"avg": 5.0, "max": 6.0}
    assert s.cpu["ffmpeg"] == {"avg": 30.0, "max": 30.0}  # null (fresh process) skipped
    assert s.wifi is not None and s.wifi["signal_dbm"]["min"] == -73
    assert s.peak == {"loudest_db": -10.3, "blocks": 1, "blocks_above_-1dBFS": 0}
    assert s.passed
    text = render(s)
    assert "RESULT: PASS" in text and "power save off" in text


def test_failures_are_detected() -> None:
    recs = records(PI_RUN)
    extra = [
        {"ts": 1794144820.0, "kind": "event", "event": "cast_state", "player_state": "BUFFERING",
         "after_first_playing": True},
        {"ts": 1794144821.0, "kind": "event", "event": "encoder_restart", "delay_s": 1},
        {"ts": 1794144822.0, "kind": "event", "event": "client_disconnect", "reason": "slow"},
        {"ts": 1794144823.0, "kind": "health", "throttled": "0x50005", "wifi": None},
        {"ts": 1794144824.0, "kind": "event", "event": "ffmpeg_xrun", "line": "ALSA buffer xrun."},
    ]  # fmt: skip
    run = sorted(recs + extra, key=lambda r: r["ts"])
    s = summarize(split_runs(run)[0], min_minutes=60.0)
    failed = {name for name, ok in s.checks.items() if not ok}
    assert failed == {
        "played >= 60 min",
        "0 encoder restarts",
        "0 client drops",
        "0 BUFFERING after first PLAYING (+5 s grace)",
        "get_throttled always 0",
    }
    assert s.counts["ffmpeg_xruns"] == 1
    assert "RESULT: FAIL" in render(s)


def test_last_run_selected_by_default(tmp_path: Path) -> None:
    from vinylcast.soak import main

    path = tmp_path / "m.jsonl"
    path.write_text(PI_RUN.strip() + "\n" + PI_RUN.strip() + "\n")
    assert main([str(path), "--no-kernel-log", "--min-minutes", "1"]) == 0


def test_decode_throttled() -> None:
    from vinylcast.metrics import decode_throttled

    # Martin's Pi, 2026-11-08: 0x50000
    assert decode_throttled(0x50000) == {"now": [], "since_boot": ["under-voltage", "throttled"]}
    assert decode_throttled(0x50005)["now"] == ["under-voltage", "throttled"]
    assert decode_throttled(0) == {"now": [], "since_boot": []}


async def test_throttled_event_only_on_change(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    from conftest import events

    from vinylcast.metrics import Metrics, PiHealth

    values = iter(["0x0", "0x50000", "0x50000", "0x50005", "0x50005"])
    health = PiHealth(Metrics(tmp_path / "m.jsonl"), interval_s=0)

    async def fake_collect() -> dict:
        return {"throttled": next(values), "net_iface": None, "wifi": None}

    monkeypatch.setattr(health, "collect", fake_collect)
    task = asyncio.create_task(health.run())
    for _ in range(50):
        await asyncio.sleep(0)
    task.cancel()
    evs = events(tmp_path / "m.jsonl", "pi_throttled")
    assert [e["value"] for e in evs] == ["0x50000", "0x50005"]
    assert evs[0]["since_boot"] == ["under-voltage", "throttled"] and evs[0]["now"] == []


def test_startup_rebuffer_within_grace_does_not_fail() -> None:
    """Martin's r0-speaker.jsonl: PLAYING at .389, BUFFERING at .875 (+0.49 s), PLAYING at
    803.378 -> counted as startup rebuffer, not a failure. Later BUFFERING still fails."""
    recs = records(PI_RUN)
    first = 1794144795.666
    blip = [{"ts": first + 0.49, "kind": "event", "event": "cast_state",
             "player_state": "BUFFERING", "after_first_playing": True},
            {"ts": first + 0.99, "kind": "event", "event": "cast_state",
             "player_state": "PLAYING", "after_first_playing": True}]  # fmt: skip
    s = summarize(split_runs(sorted(recs + blip, key=lambda r: r["ts"]))[0], min_minutes=1.0)
    assert s.counts["startup_rebuffers"] == 1 and s.counts["buffering_after_playing"] == 0
    assert s.passed
    late = [{"ts": first + 30, "kind": "event", "event": "cast_state",
             "player_state": "BUFFERING", "after_first_playing": True}]  # fmt: skip
    s = summarize(split_runs(sorted(recs + blip + late, key=lambda r: r["ts"]))[0], 1.0)
    assert s.counts["buffering_after_playing"] == 1 and not s.passed
