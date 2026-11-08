"""Soak-test summary over a metrics JSONL file (M5/M6).

A file may hold several runs; each starts at a `service_start` event. The summary covers one
run, judges it against PLAN.md's M5 criteria, and reports the proxy signals for dropouts.

Window: events after the *stop marker* (the receiver going IDLE/CANCELLED after playing, a
`cast_stop` or `service_stop`, whichever comes first) belong to the intended shutdown and do
not count as failures.
"""

from __future__ import annotations

import re
import statistics
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vinylcast.metrics import read_records

STARTUP_GRACE_S = 5.0

KERNEL_PATTERNS = {
    "usb_data_lost": re.compile(r"Data may have been lost"),
    "usb_disconnect": re.compile(r"usb \S+: USB disconnect"),
    "undervoltage": re.compile(r"[Uu]ndervoltage|under-voltage"),
}


def split_runs(records: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    runs: list[list[dict[str, Any]]] = []
    for r in records:
        if r.get("kind") == "event" and r.get("event") == "service_start":
            runs.append([])
        if runs:
            runs[-1].append(r)
    return runs


def _stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"avg": round(statistics.fmean(values), 1), "max": round(max(values), 1)}


def kernel_log_counts(start: float, end: float) -> dict[str, int] | None:
    """Count kernel messages of interest in [start, end] via journalctl -k (None if the
    journal is not readable, e.g. not on the Pi or no permission)."""
    try:
        out = subprocess.run(
            ["journalctl", "-k", "-q", "--no-pager", "-o", "short-unix",
             "--since", f"@{int(start)}", "--until", f"@{int(end) + 1}"],
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return {k: len(p.findall(out.stdout)) for k, p in KERNEL_PATTERNS.items()}


@dataclass
class Summary:
    config: dict[str, Any]
    start: float
    end: float
    first_playing: float | None
    stop_marker: float
    startup_latency_s: float | None
    counts: dict[str, int]
    cpu: dict[str, dict[str, float] | None]
    rss_max_mb: dict[str, float | None]
    wifi: dict[str, Any] | None
    throttled_values: list[str]
    peak: dict[str, Any] | None
    kernel: dict[str, int] | None
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def playing_s(self) -> float:
        return 0.0 if self.first_playing is None else self.stop_marker - self.first_playing

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def summarize(
    run: list[dict[str, Any]], min_minutes: float = 60.0, kernel: dict[str, int] | None = None
) -> Summary:
    events = [r for r in run if r.get("kind") == "event"]
    samples = [r for r in run if r.get("kind") == "sample"]
    health = [r for r in run if r.get("kind") == "health"]
    start = run[0]["ts"]
    end = run[-1]["ts"]
    config = events[0].get("config", {}) if events else {}

    markers = [
        e["ts"]
        for e in events
        if e["event"] in ("cast_stop", "service_stop")
        or (
            e["event"] == "cast_state"
            and e.get("idle_reason") == "CANCELLED"
            and e.get("after_first_playing")
        )  # fmt: skip
    ]
    stop_marker = min(markers) if markers else end
    window = [e for e in events if e["ts"] < stop_marker]

    def count(name: str, **match: Any) -> int:
        return sum(
            1 for e in window if e["event"] == name and all(e.get(k) == v for k, v in match.items())
        )

    playing = [
        e["ts"] for e in events if e["event"] == "cast_state" and e.get("player_state") == "PLAYING"
    ]
    startup = [e.get("latency_s") for e in events if e["event"] == "cast_startup"]
    after = [e for e in window if e["event"] == "cast_state" and e.get("after_first_playing")]
    first_play = min(playing, default=None)
    # Martin, 2026-11-08: HESTON 120 always rebuffers once ~0.5 s after starting. A BUFFERING
    # within STARTUP_GRACE_S of the first PLAYING is reported separately and does not fail.
    buffering = [e for e in after if e.get("player_state") == "BUFFERING"]
    startup_rebuffers = sum(
        1 for e in buffering if first_play is not None and e["ts"] - first_play <= STARTUP_GRACE_S
    )
    counts = {
        "encoder_restarts": count("encoder_restart"),
        "encoder_stalls": count("encoder_stall"),
        "ffmpeg_xruns": count("ffmpeg_xrun"),
        "ffmpeg_warnings": count("ffmpeg_warning"),
        "client_drops": sum(
            1 for e in window if e["event"] == "client_disconnect" and e.get("reason") != "shutdown"
        ),
        "slow_clients": count("client_slow"),
        "buffering_after_playing": len(buffering) - startup_rebuffers,
        "startup_rebuffers": startup_rebuffers,
        "idle_after_playing": sum(1 for e in after if e.get("player_state") == "IDLE"),
        "cast_connection_problems": sum(
            1
            for e in window
            if e["event"] == "cast_connection"
            and e.get("status") not in ("CONNECTED", "CONNECTING")
        ),  # fmt: skip
        "cast_errors": count("cast_load_failed") + count("cast_launch_error"),
        "framing_skipped": count("framing_skipped"),
        "throttled_events": count("pi_throttled"),
        "http_requests": count("http_request"),
    }

    def proc_values(name: str, key: str) -> list[float]:
        return [s[name][key] for s in samples if s.get(name) and s[name].get(key) is not None]

    cpu = {p: _stats(proc_values(p, "cpu_pct")) for p in ("python", "ffmpeg")}
    rss = {p: max(proc_values(p, "rss_mb"), default=None) for p in ("python", "ffmpeg")}

    wifi_recs = [h["wifi"] for h in health if h.get("wifi")]
    wifi = None
    if wifi_recs:
        signals = [w["signal_dbm"] for w in wifi_recs if "signal_dbm" in w]
        tx = [w["tx_mbit"] for w in wifi_recs if "tx_mbit" in w]
        sig = None
        if signals:
            sig = {"min": min(signals), "avg": round(statistics.fmean(signals), 1),
                   "max": max(signals)}  # fmt: skip
        wifi = {
            "samples": len(wifi_recs),
            "disconnected_samples": sum(1 for w in wifi_recs if not w.get("connected", True)),
            "signal_dbm": sig,
            "tx_mbit_min": min(tx, default=None),
            "freq_mhz": sorted({w["freq_mhz"] for w in wifi_recs if "freq_mhz" in w}),
            "power_save_on": any(w.get("power_save") for w in wifi_recs),
        }  # fmt: skip
    throttled = sorted({h["throttled"] for h in health if h.get("throttled") not in (None, "0x0")})

    peaks = [e for e in window if e["event"] == "input_peak" and e.get("peak_db") is not None]
    peak = None
    if peaks:
        peak = {
            "loudest_db": max(e["peak_db"] for e in peaks),
            "blocks": len(peaks),
            "blocks_above_-1dBFS": sum(1 for e in peaks if e.get("clip_risk")),
        }

    s = Summary(
        config=config, start=start, end=end, first_playing=first_play,
        stop_marker=stop_marker, startup_latency_s=startup[0] if startup else None,
        counts=counts, cpu=cpu, rss_max_mb=rss, wifi=wifi, throttled_values=throttled,
        peak=peak, kernel=kernel,
    )  # fmt: skip
    s.checks = {
        "reached PLAYING": s.first_playing is not None,
        f"played >= {min_minutes:g} min": s.playing_s >= min_minutes * 60,
        "0 encoder restarts": counts["encoder_restarts"] == 0,
        "0 client drops": counts["client_drops"] == 0,
        f"0 BUFFERING after first PLAYING (+{STARTUP_GRACE_S:g} s grace)": (
            counts["buffering_after_playing"] == 0
        ),
        "get_throttled always 0": not throttled and counts["throttled_events"] == 0,
    }
    return s


def _fmt(v: Any) -> str:
    return "n/a" if v is None else str(v)


def render(s: Summary) -> str:
    c = s.config
    lines = [
        f"Run: {c.get('source')} -> {c.get('codec')} -> {c.get('cast_target')}  "
        f"(sample_rate {c.get('sample_rate')}, mp3 {c.get('mp3_bitrate')})",
        f"Duration: {(s.end - s.start) / 60:.1f} min total, {s.playing_s / 60:.1f} min playing "
        f"(first PLAYING to stop)",
        f"Startup latency (play_media -> PLAYING): {_fmt(s.startup_latency_s)} s",
        "",
        "Criteria (PLAN.md M5):",
    ]
    lines += [f"  [{'PASS' if ok else 'FAIL'}] {name}" for name, ok in s.checks.items()]
    lines += ["", "CPU % (avg / max per 5 s sample), RSS max:"]
    for p in ("python", "ffmpeg"):
        st = s.cpu[p]
        cpu = "n/a" if st is None else f"{st['avg']:5.1f} / {st['max']:5.1f}"
        lines.append(f"  {p:7s} {cpu}   RSS {_fmt(s.rss_max_mb[p])} MB")
    lines += ["", "Events before stop (dropout proxies):"]
    lines += [f"  {k:26s} {v}" for k, v in s.counts.items()]
    lines += ["", "Kernel log in the run window (journalctl -k):"]
    if s.kernel is None:
        lines.append("  n/a (journal not readable here)")
    else:
        lines += [f"  {k:26s} {v}" for k, v in s.kernel.items()]
    lines += ["", f"Pi throttled values seen: {', '.join(s.throttled_values) or 'none'}"]
    if s.wifi:
        w = s.wifi
        sig = w["signal_dbm"]
        lines.append(
            f"Wi-Fi: signal {sig['min']}/{sig['avg']}/{sig['max']} dBm (min/avg/max), "
            f"tx >= {w['tx_mbit_min']} Mbit/s, freq {w['freq_mhz']}, "
            f"power save {'ON' if w['power_save_on'] else 'off'}, "
            f"{w['disconnected_samples']} disconnected samples"
            if sig
            else f"Wi-Fi: {w}"
        )
    else:
        lines.append("Wi-Fi: not used (or no data)")
    if s.peak:
        lines.append(
            f"Input peak: loudest {s.peak['loudest_db']} dBFS over {s.peak['blocks']} blocks; "
            f"{s.peak['blocks_above_-1dBFS']} blocks above -1 dBFS"
        )
    lines += [
        "",
        f"RESULT: {'PASS' if s.passed else 'FAIL'}",
        "Reminder: the receiver gives no direct dropout signal; add the listener's notes of "
        "audible gaps (with times) to the report.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Summarise a VinylCast soak-test run.")
    ap.add_argument("metrics", type=Path, help="metrics JSONL file")
    ap.add_argument("--run", type=int, default=-1, help="run index in the file (default: last)")
    ap.add_argument("--min-minutes", type=float, default=60.0)
    ap.add_argument("--no-kernel-log", action="store_true", help="skip journalctl")
    args = ap.parse_args(argv)
    runs = split_runs(read_records(args.metrics))
    if not runs:
        print(f"No runs (service_start events) in {args.metrics}")
        return 2
    run = runs[args.run]
    kernel = None if args.no_kernel_log else kernel_log_counts(run[0]["ts"], run[-1]["ts"])
    s = summarize(run, args.min_minutes, kernel)
    print(f"{args.metrics}: run {args.run % len(runs) + 1} of {len(runs)}")
    print(render(s))
    return 0 if s.passed else 1
