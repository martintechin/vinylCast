"""One test per rule in docs/PLAN-rev1.md "Auto-cast rules" (plus edge cases)."""

from vinylcast.controller import Cast, Controller, Notify, State, Stop

T = "HESTON 120"


def ctl(**kw) -> Controller:  # type: ignore[no-untyped-def]
    kw.setdefault("target", T)
    return Controller(**kw)


def playing(c: Controller, now: float = 0.0) -> None:
    assert c.signal_on(now) == [Cast(T, "needle_drop")]
    c.cast_started(now + 2)
    assert c.state == State.PLAYING


def tick_until(c: Controller, start: float, end: float, step: float = 1.0) -> list:  # type: ignore[type-arg]
    actions, t = [], start
    while t <= end:
        actions += [(t, a) for a in c.tick(t)]
        t += step
    return actions


def test_needle_drop_casts_to_last_speaker() -> None:
    c = ctl()
    assert c.signal_on(10.0) == [Cast(T, "needle_drop")]
    assert c.state == State.STARTING
    assert c.cast_started(12.0) == [] and c.state == State.PLAYING


def test_stops_after_full_silence_not_before() -> None:
    c = ctl(stop_after_silence_s=180)
    playing(c)
    c.signal_off(100.0)
    acts = tick_until(c, 100.0, 400.0)
    assert acts == [(280.0, Stop("silence"))]
    assert c.state == State.STOPPING
    c.stopped(281.0)
    assert c.state == State.IDLE


def test_side_flip_keeps_playing() -> None:
    c = ctl()
    playing(c)
    c.signal_off(100.0)
    assert tick_until(c, 100.0, 160.0) == []  # 60 s flip
    assert c.signal_on(160.0) == []  # already playing: no second cast
    assert tick_until(c, 160.0, 1000.0) == []
    assert c.state == State.PLAYING


def test_next_record_after_auto_stop_casts_again() -> None:
    c = ctl()
    playing(c)
    c.signal_off(100.0)
    tick_until(c, 100.0, 300.0)
    c.stopped(281.0)
    assert c.signal_on(900.0) == [Cast(T, "needle_drop")]


def test_manual_stop_holds_until_record_ends() -> None:
    c = ctl()
    playing(c)
    assert c.stop(50.0) == [Stop("manual")]
    c.cast_ended(51.0, "cancelled")  # receiver confirms
    assert c.state == State.IDLE and c.hold == "manual"
    # Same record continues: track gap, music again -> no auto-cast.
    c.signal_off(60.0)
    tick_until(c, 60.0, 70.0)
    assert c.signal_on(70.0) == []
    # Record ends; full silence period passes -> hold released; next record casts.
    c.signal_off(1200.0)
    tick_until(c, 1200.0, 1380.0)
    assert c.hold is None
    assert c.signal_on(1500.0) == [Cast(T, "needle_drop")]


def test_stop_from_google_home_is_a_manual_stop() -> None:
    c = ctl()
    playing(c)
    acts = c.cast_ended(30.0, "cancelled")
    assert c.state == State.IDLE and c.hold == "manual"
    assert acts and isinstance(acts[0], Notify)
    assert c.signal_on(40.0) == []


def test_other_app_took_over_is_respected() -> None:
    c = ctl()
    playing(c)
    c.cast_ended(30.0, "taken_over")
    assert c.hold == "taken_over" and c.signal_on(40.0) == []


def test_busy_speaker_is_not_taken_over() -> None:
    c = ctl()
    c.signal_on(0.0)
    acts = c.cast_failed(1.0, "busy")
    assert c.state == State.IDLE and c.hold == "busy"
    assert acts == [Notify("warning", f"{T} is busy with another app; not taking over")]
    # No retry on every track of the same record.
    c.signal_off(200.0)
    tick_until(c, 200.0, 210.0)
    assert c.signal_on(210.0) == []


def test_speaker_not_found() -> None:
    c = ctl()
    c.signal_on(0.0)
    acts = c.cast_failed(30.0, "not_found")
    assert c.hold == "unavailable" and acts[0].level == "error"  # type: ignore[union-attr]


def test_receiver_error_recovers_within_budget() -> None:
    c = ctl(recast_max=3, recast_window_s=600)
    playing(c)
    for i in range(3):
        assert c.cast_ended(100.0 + i, "error") == [Cast(T, "recover")]
        c.cast_started(100.5 + i)
    acts = c.cast_ended(110.0, "lost")
    assert c.state == State.IDLE and c.hold == "errors"
    assert isinstance(acts[0], Notify) and acts[0].level == "error"


def test_recast_budget_refills_after_window() -> None:
    c = ctl(recast_max=1, recast_window_s=600)
    playing(c)
    assert c.cast_ended(100.0, "error") == [Cast(T, "recover")]
    c.cast_started(101.0)
    assert c.cast_ended(800.0, "error") == [Cast(T, "recover")]  # >600 s later


def test_no_recast_without_signal() -> None:
    c = ctl()
    playing(c)
    c.signal_off(50.0)
    acts = c.cast_ended(60.0, "lost")
    assert c.state == State.IDLE and not any(isinstance(a, Cast) for a in acts)


def test_auto_cast_disabled() -> None:
    c = ctl(auto_cast=False)
    assert c.signal_on(0.0) == []
    assert c.play(5.0) == [Cast(T, "manual")]
    c.cast_started(7.0)
    c.signal_off(10.0)
    assert tick_until(c, 10.0, 1000.0) == []  # no automatic stop either
    assert c.stop(1000.0) == [Stop("manual")]


def test_no_speaker_chosen() -> None:
    c = Controller()
    acts = c.signal_on(0.0)
    assert len(acts) == 1 and isinstance(acts[0], Notify)
    assert c.play(1.0) == [Notify("warning", "Choose a speaker first")]


def test_manual_play_in_silence_starts_silence_timer_at_playback() -> None:
    c = ctl()
    c.signal_off(0.0)  # silent for a long time already
    c.stop(10.0)  # hold
    assert c.play(5000.0) == [Cast(T, "manual")]  # play clears the hold
    c.cast_started(5002.0)
    assert tick_until(c, 5002.0, 5100.0) == []  # not stopped at once
    assert [a for _, a in tick_until(c, 5100.0, 5300.0)] == [Stop("silence")]


def test_switch_speaker_while_playing() -> None:
    c = ctl()
    playing(c)
    assert c.play(50.0, "Tvättstugan") == [Stop("switch"), Cast("Tvättstugan", "switch")]
    assert c.target == "Tvättstugan" and c.state == State.STARTING


def test_play_same_speaker_while_playing_is_noop() -> None:
    c = ctl()
    playing(c)
    assert c.play(50.0, T) == []


def test_transitions_are_reported() -> None:
    log: list[tuple[str, str, str]] = []
    c = ctl(on_transition=lambda a, b, r: log.append((a.value, b.value, r)))
    playing(c)
    c.stop(10.0)
    assert log == [
        ("idle", "starting", "needle_drop"),
        ("starting", "playing", "receiver playing"),
        ("playing", "stopping", "manual"),
    ]


# ---- R5 recovery: speaker power-cycled / Wi-Fi access point restarted --------------------


def lost_and_unreachable(c: Controller, now: float = 100.0) -> None:
    """Connection lost while the record plays; the immediate re-cast cannot find the speaker."""
    assert c.cast_ended(now, "lost") == [Cast(T, "recover")]
    acts = c.cast_failed(now + 10, "not_found")
    assert acts == [Notify("warning", f"Lost {T}; reconnecting…")]
    assert c.state == State.IDLE and c.recovering and c.hold is None


def test_speaker_power_cycle_recovers_with_retries() -> None:
    c = ctl()
    playing(c)
    lost_and_unreachable(c, 100.0)  # retry 2 s after the failure at 110
    assert tick_until(c, 110.0, 111.0) == []
    assert [a for _, a in tick_until(c, 112.0, 112.0)] == [Cast(T, "recover")]
    assert c.cast_failed(122.0, "not_found") == []  # still down: retry in 5 s, no new note
    assert [a for _, a in tick_until(c, 123.0, 127.0)] == [Cast(T, "recover")]
    c.cast_started(130.0)  # speaker back
    assert c.state == State.PLAYING and not c.recovering and c.hold is None


def test_recovery_gives_up_after_timeout() -> None:
    c = ctl(recover_timeout_s=300)
    playing(c)
    lost_and_unreachable(c, 100.0)
    t, attempts = 110.0, 0
    while c.recovering and t < 1000:
        for _, a in tick_until(c, t, t + 30):
            if isinstance(a, Cast):
                attempts += 1
                c.cast_failed(t + 1, "not_found")
        t += 31
    assert not c.recovering and c.hold == "errors" and c.state == State.IDLE
    assert 6 <= attempts <= 15  # retried for ~5 minutes with growing delays, then stopped


def test_recovery_stops_when_the_record_ends() -> None:
    c = ctl()
    playing(c)
    lost_and_unreachable(c)
    c.signal_off(115.0)
    assert not c.recovering and c.hold is None
    assert tick_until(c, 115.0, 200.0) == []  # no retries without music
    assert c.signal_on(1000.0) == [Cast(T, "needle_drop")]  # next record casts normally


def test_recovery_respects_someone_else_taking_the_speaker() -> None:
    c = ctl()
    playing(c)
    lost_and_unreachable(c)
    tick_until(c, 110.0, 112.0)
    c.cast_failed(113.0, "busy")
    assert not c.recovering and c.hold == "busy"


def test_manual_stop_or_play_ends_recovery() -> None:
    c = ctl()
    playing(c)
    lost_and_unreachable(c)
    c.stop(111.0)
    assert not c.recovering and c.hold == "manual" and tick_until(c, 111.0, 200.0) == []
    c2 = ctl()
    playing(c2)
    lost_and_unreachable(c2)
    assert c2.play(111.0) == [Cast(T, "manual")] and not c2.recovering
