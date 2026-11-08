import json
from pathlib import Path

from vinylcast.autocast_report import build, main, render
from vinylcast.soak import split_runs

# Martin's R1 run on the Pi (r1.jsonl, second daemon start), events as logged.
R1 = """
{"ts":1794164700.0,"kind":"event","event":"service_start","config":{"detect_on_s":1.5,"stop_after_silence_s":180.0},"mode":"daemon"}
{"ts":1794164743.929,"kind":"event","event":"signal","on":true,"rms_db":-25.7}
{"ts":1794164743.93,"kind":"event","event":"ctl_state","old":"idle","new":"starting","reason":"needle_drop"}
{"ts":1794164747.584,"kind":"event","event":"cast_startup","target":"HESTON 120","latency_s":3.003}
{"ts":1794164747.585,"kind":"event","event":"ctl_state","old":"starting","new":"playing","reason":"receiver playing"}
{"ts":1794164760.63,"kind":"event","event":"signal","on":false,"rms_db":-54.9}
{"ts":1794164764.734,"kind":"event","event":"signal","on":true,"rms_db":-30.7}
{"ts":1794164769.633,"kind":"event","event":"signal","on":false,"rms_db":-57.2}
{"ts":1794164781.134,"kind":"event","event":"signal","on":true,"rms_db":-27.0}
{"ts":1794164796.336,"kind":"event","event":"signal","on":false,"rms_db":-58.1}
{"ts":1794164976.382,"kind":"event","event":"ctl_state","old":"playing","new":"stopping","reason":"silence"}
{"ts":1794164976.42,"kind":"event","event":"cast_ended","target":"HESTON 120","reason":"cancelled"}
{"ts":1794164976.422,"kind":"event","event":"ctl_state","old":"stopping","new":"idle","reason":"stopped"}
"""


def recs(text: str) -> list[dict]:
    return [json.loads(line) for line in text.strip().splitlines()]


def test_martins_r1_cycle() -> None:
    rep = build(split_runs(recs(R1))[0])
    (c,) = rep.cycles
    assert c.reason == "needle_drop" and c.target == "HESTON 120"
    assert c.needle_to_playing(1.5) == 5.16  # 747.585 - 743.929 + 1.5
    assert c.silence_to_stop() == 180.0  # 976.382 - 796.336
    assert c.gaps == [4.1, 11.5]  # silences while playing that did not stop it
    assert all(rep.checks.values())
    text = render(rep)
    assert "needle_drop" in text and "5.2 s" in text and "180 s" in text


def test_slow_start_and_false_start_fail() -> None:
    records = recs(R1)
    # A second cycle: needle drop with 6 s to playing (> 6 s with detect_on_s added).
    records += [
        {"ts": 1794165000.0, "kind": "event", "event": "signal", "on": True},
        {"ts": 1794165000.1, "kind": "event", "event": "ctl_state", "old": "idle",
         "new": "starting", "reason": "needle_drop"},
        {"ts": 1794165006.0, "kind": "event", "event": "ctl_state", "old": "starting",
         "new": "playing", "reason": "receiver playing"},
    ]  # fmt: skip
    rep = build(split_runs(records)[0], idle_window=(1794164990.0, 1794165010.0))
    failed = {k for k, ok in rep.checks.items() if not ok}
    assert any(k.startswith("every needle drop") for k in failed)
    assert "no signal or automatic start in the idle window" in failed
    assert rep.cycles[-1].end_reason is None  # still playing
    assert "still playing" in render(rep)


def test_manual_stop_and_notifications() -> None:
    records = recs(R1)[:6] + [
        {"ts": 1794164770.0, "kind": "event", "event": "ctl_state", "old": "playing",
         "new": "stopping", "reason": "manual"},
        {"ts": 1794164770.1, "kind": "event", "event": "cast_end_ignored", "why": "our stop"},
        {"ts": 1794164770.2, "kind": "event", "event": "ctl_state", "old": "stopping",
         "new": "idle", "reason": "stopped"},
        {"ts": 1794164800.0, "kind": "event", "event": "notify", "level": "warning",
         "message": "HESTON 120 is busy with another app; not taking over"},
    ]  # fmt: skip
    rep = build(split_runs(records)[0])
    assert rep.cycles[0].end_reason == "manual" and rep.cycles[0].silence_to_stop() is None
    assert rep.ignored_ends == 1 and "busy" in rep.holds[0][1]


def test_cli_exit_code(tmp_path: Path) -> None:
    path = tmp_path / "m.jsonl"
    path.write_text(R1.strip() + "\n")
    assert main([str(path)]) == 0
