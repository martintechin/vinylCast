import math

from vinylcast.detector import Level, SignalDetector


def feed(d: SignalDetector, start: float, end: float, rms: float, step: float = 0.1) -> list:  # type: ignore[type-arg]
    out, t = [], start
    while t < end - 1e-9:
        ev = d.feed(Level(round(t, 3), rms, rms + 12))
        if ev:
            out.append((round(t, 1), ev))
        t += step
    return out


def test_on_after_sustained_signal() -> None:
    d = SignalDetector(-50, on_after_s=1.5)
    assert feed(d, 0, 3, -30) == [(1.5, "on")]
    assert d.signal


def test_click_shorter_than_on_after_is_ignored() -> None:
    d = SignalDetector(-50, on_after_s=1.5)
    assert feed(d, 0, 2, -70) == []
    assert feed(d, 2, 2.3, -20) == []  # 0.3 s click
    assert feed(d, 2.3, 10, -70) == []
    assert not d.signal


def test_hysteresis_band_keeps_state() -> None:
    d = SignalDetector(-50, on_after_s=1.0, off_after_s=1.0, hysteresis_db=6)
    feed(d, 0, 2, -40)
    assert d.signal
    assert feed(d, 2, 10, -53) == []  # below on-threshold but above -56: still "on"
    assert feed(d, 10, 12, -60) == [(11.0, "off")]


def test_quiet_dip_shorter_than_off_after_keeps_on() -> None:
    d = SignalDetector(-50, on_after_s=1.0, off_after_s=1.0)
    feed(d, 0, 2, -40)
    assert feed(d, 2, 2.5, -80) == []
    assert feed(d, 2.5, 5, -40) == []
    assert d.signal


def test_silence_and_nan_count_as_below() -> None:
    d = SignalDetector(-50, on_after_s=0.5, off_after_s=0.5)
    feed(d, 0, 1, -40)
    assert feed(d, 1, 2, -math.inf) == [(1.5, "off")]
    feed(d, 2, 3, -40)
    assert feed(d, 3, 4, math.nan) == [(3.5, "off")]


def test_history_is_bounded() -> None:
    d = SignalDetector(-50, history_s=5)
    feed(d, 0, 20, -40)
    assert d.history[0].t >= 14.9 and d.latest is not None and d.latest.t > 19.8
