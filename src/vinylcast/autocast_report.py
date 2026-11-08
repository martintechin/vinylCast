"""R4 evaluation: needle-drop cycles from a daemon metrics file.

    python scripts/r4_summary.py FILE.jsonl [--idle-from HH:MM --idle-to HH:MM]

A cycle runs from the controller leaving `idle` (needle drop, manual play, switch) until it
is idle again. Per cycle: how it started, music -> speaker playing (needle drop: the music
began `detect_on_s` before the detector reported it), how it ended, and for silence stops
the time from the last music to the stop. Side flips = silences during playback that did
not stop it. The optional idle window counts every detected signal / automatic start
inside it (false starts; the turntable must be off then). Times are local clock times.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from vinylcast.soak import split_runs

MAX_START_S = 6.0  # PLAN-rev1 R4: music on the speaker within 6 s of needle drop
STOP_TOLERANCE_S = 10.0  # stop after stop_after_silence_s +- 10 s


def clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


@dataclass
class Cycle:
    start_ts: float
    reason: str
    target: str | None = None
    signal_ts: float | None = None  # detector "on" that triggered a needle drop
    playing_ts: float | None = None
    end_ts: float | None = None
    end_reason: str | None = None
    last_off_ts: float | None = None  # last "signal off" before a silence stop
    gaps: list[float] = field(default_factory=list)  # silences while playing (side flips)

    def needle_to_playing(self, detect_on_s: float) -> float | None:
        if self.reason != "needle_drop" or self.signal_ts is None or self.playing_ts is None:
            return None
        return round(self.playing_ts - self.signal_ts + detect_on_s, 2)

    def silence_to_stop(self) -> float | None:
        if self.end_reason != "silence" or self.last_off_ts is None or self.end_ts is None:
            return None
        return round(self.end_ts - self.last_off_ts, 1)


@dataclass
class Report:
    config: dict[str, Any]
    cycles: list[Cycle]
    holds: list[tuple[float, str]]  # notify events (warnings / errors / info)
    ignored_ends: int
    idle_window: tuple[float, float] | None = None
    idle_signals: list[float] = field(default_factory=list)
    idle_starts: list[float] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)


def build(run: Iterable[dict[str, Any]], idle_window: tuple[float, float] | None = None) -> Report:
    events = [r for r in run if r.get("kind") == "event"]
    config = events[0].get("config", {}) if events else {}
    cycles: list[Cycle] = []
    current: Cycle | None = None
    last_on: float | None = None
    off_since: float | None = None
    holds: list[tuple[float, str]] = []
    ignored = 0
    idle_signals: list[float] = []
    idle_starts: list[float] = []

    def in_idle(ts: float) -> bool:
        return idle_window is not None and idle_window[0] <= ts <= idle_window[1]

    for e in events:
        ts, name = e["ts"], e["event"]
        if name == "signal":
            if e.get("on"):
                last_on = ts
                if in_idle(ts):
                    idle_signals.append(ts)
                if current is not None and off_since is not None and current.playing_ts:
                    current.gaps.append(round(ts - off_since, 1))
                off_since = None
            else:
                off_since = ts
        elif name == "ctl_state":
            old, new, why = e.get("old"), e.get("new"), e.get("reason", "")
            if old == "idle" and new == "starting":
                current = Cycle(ts, why, signal_ts=last_on if why == "needle_drop" else None)
                cycles.append(current)
                if why == "needle_drop" and in_idle(ts):
                    idle_starts.append(ts)
            elif current is not None and new == "playing" and current.playing_ts is None:
                current.playing_ts = ts
            elif current is not None and new == "stopping" and current.end_reason is None:
                current.end_reason = why
                current.last_off_ts = off_since
            elif current is not None and new == "idle":
                current.end_ts = ts
                if current.end_reason is None:
                    current.end_reason = why  # e.g. "ended: cancelled", "cast failed: busy"
                current = None
        elif name == "cast_startup" and current is not None:
            current.target = e.get("target")
        elif name == "notify":
            holds.append((ts, f"{e.get('level')}: {e.get('message')}"))
        elif name == "cast_end_ignored":
            ignored += 1
    # A "stopping" end is timed at the stop decision; use that, not the receiver's idle.
    for c in cycles:
        if c.end_reason == "silence":
            stop_events = [
                e["ts"] for e in events
                if e["event"] == "ctl_state" and e.get("new") == "stopping"
                and c.start_ts <= e["ts"] <= (c.end_ts or e["ts"])
            ]  # fmt: skip
            if stop_events:
                c.end_ts = stop_events[0]
    rep = Report(config, cycles, holds, ignored, idle_window, idle_signals, idle_starts)
    detect_on = float(config.get("detect_on_s", 1.5))
    stop_after = float(config.get("stop_after_silence_s", 180.0))
    drops = [c.needle_to_playing(detect_on) for c in cycles if c.reason == "needle_drop"]
    stops = [c.silence_to_stop() for c in cycles if c.end_reason == "silence"]
    rep.checks = {
        f"every needle drop playing within {MAX_START_S:g} s ({len(drops)} drops)":
            bool(drops) and all(d is not None and d <= MAX_START_S for d in drops),
        f"silence stops at {stop_after:g} +- {STOP_TOLERANCE_S:g} s ({len(stops)} stops)":
            all(s is not None and abs(s - stop_after) <= STOP_TOLERANCE_S for s in stops),
    }  # fmt: skip
    if idle_window is not None:
        rep.checks["no signal or automatic start in the idle window"] = not (
            idle_signals or idle_starts
        )
    return rep


def render(rep: Report) -> str:
    detect_on = float(rep.config.get("detect_on_s", 1.5))
    lines = [f"{'start':8s}  {'how':12s} {'speaker':14s} {'needle->play':>12s}  "
             f"{'ended':24s} {'silence->stop':>13s}  side flips/gaps (s)"]  # fmt: skip
    for c in rep.cycles:
        n2p = c.needle_to_playing(detect_on)
        s2s = c.silence_to_stop()
        n2p_txt = "-" if n2p is None else f"{n2p:.1f} s"
        s2s_txt = "-" if s2s is None else f"{s2s:.0f} s"
        ended = (c.end_reason or "still playing")[:24]
        gaps = ", ".join(f"{g:g}" for g in c.gaps) or "-"
        lines.append(
            f"{clock(c.start_ts):8s}  {c.reason[:12]:12s} {(c.target or '-')[:14]:14s} "
            f"{n2p_txt:>12s}  {ended:24s} {s2s_txt:>13s}  {gaps}"
        )
    if not rep.cycles:
        lines.append("(no cycles)")
    lines += ["", f"Notifications: {len(rep.holds)}"]
    lines += [f"  {clock(ts)}  {msg}" for ts, msg in rep.holds]
    lines.append(f"Own end reports ignored (our stops, replaced connections): {rep.ignored_ends}")
    if rep.idle_window is not None:
        a, b = rep.idle_window
        lines.append(f"Idle window {clock(a)}-{clock(b)}: {len(rep.idle_signals)} signals, "
                     f"{len(rep.idle_starts)} automatic starts")  # fmt: skip
        lines += [f"  signal at {clock(t)}" for t in rep.idle_signals]
    lines += ["", "Criteria (PLAN-rev1 R4):"]
    lines += [f"  [{'PASS' if ok else 'FAIL'}] {name}" for name, ok in rep.checks.items()]
    lines.append("Also check by ear/notes: side flip kept playing, manual stop respected "
                 "until the next record.")  # fmt: skip
    return "\n".join(lines)


def _parse_hhmm(day_ts: float, hhmm: str) -> float:
    t = time.localtime(day_ts)
    h, m = (int(x) for x in hhmm.split(":"))
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1))


def main(argv: list[str] | None = None) -> int:
    import argparse
    from pathlib import Path

    from vinylcast.metrics import read_records

    ap = argparse.ArgumentParser(description="Needle-drop cycles from a daemon metrics file.")
    ap.add_argument("metrics", type=Path)
    ap.add_argument("--run", type=int, default=None, help="run index (default: all runs)")
    ap.add_argument("--idle-from", help="HH:MM, start of a turntable-off window (false starts)")
    ap.add_argument("--idle-to", help="HH:MM, end of that window")
    args = ap.parse_args(argv)
    runs = split_runs(read_records(args.metrics))
    if not runs:
        print(f"No runs in {args.metrics}")
        return 2
    chosen = runs if args.run is None else [runs[args.run]]
    records = [r for run in chosen for r in run]
    window = None
    if args.idle_from and args.idle_to:
        first = records[0]["ts"]
        a, b = _parse_hhmm(first, args.idle_from), _parse_hhmm(first, args.idle_to)
        if b < a:
            b += 86400  # window over midnight
        window = (a, b)
    rep = build(records, window)
    print(f"{args.metrics}: {len(chosen)} run(s), {len(rep.cycles)} cycle(s)")
    print(render(rep))
    return 0 if all(rep.checks.values()) else 1
