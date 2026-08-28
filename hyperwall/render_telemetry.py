"""Bounded render-path telemetry for the libmpv/Qt video surface.

The mpv frame counters tell us what the player decided to drop, but they do
not show whether the Qt render callback is arriving faster than ``paintGL``
can service it.  This module keeps small cumulative and interval counters so
live soaks can distinguish callback coalescing, slow render calls, and event
loop stalls without retaining a per-frame history.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any


_FIELDS = (
    "frame_ready",
    "paint_calls",
    "render_calls",
    "render_errors",
    "paint_total_ms",
    "paint_max_ms",
    "render_total_ms",
    "render_max_ms",
    "paint_gap_max_ms",
    "paint_gap_last_ms",
)


def _empty() -> dict[str, int | float]:
    return {
        "frame_ready": 0,
        "paint_calls": 0,
        "render_calls": 0,
        "render_errors": 0,
        "paint_total_ms": 0.0,
        "paint_max_ms": 0.0,
        "render_total_ms": 0.0,
        "render_max_ms": 0.0,
        "paint_gap_max_ms": 0.0,
        "paint_gap_last_ms": 0.0,
    }


def _duration_ms(value: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and result >= 0.0 else 0.0


class RenderTelemetry:
    """Thread-safe bounded counters for one video surface."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = _empty()
        self._interval = _empty()
        self._last_paint_ns: int | None = None

    def record_frame_ready(self) -> None:
        """Record an accepted mpv update callback."""
        with self._lock:
            self._total["frame_ready"] += 1
            self._interval["frame_ready"] += 1

    def record_paint(
        self,
        *,
        paint_ms: float,
        render_ms: float,
        rendered: bool,
        render_attempted: bool = True,
        now_ns: int | None = None,
    ) -> None:
        """Record one GUI paint and its libmpv render outcome.

        ``now_ns`` is injectable so the interval/gap behavior is deterministic
        in unit tests.  Production callers use the monotonic clock.
        """
        paint = _duration_ms(paint_ms)
        render = _duration_ms(render_ms)
        timestamp = time.perf_counter_ns() if now_ns is None else int(now_ns)
        with self._lock:
            for bucket in (self._total, self._interval):
                bucket["paint_calls"] += 1
                bucket["paint_total_ms"] += paint
                bucket["paint_max_ms"] = max(bucket["paint_max_ms"], paint)
                bucket["render_total_ms"] += render
                bucket["render_max_ms"] = max(bucket["render_max_ms"], render)
                if render_attempted:
                    if rendered:
                        bucket["render_calls"] += 1
                    else:
                        bucket["render_errors"] += 1
                if self._last_paint_ns is not None and timestamp >= self._last_paint_ns:
                    gap_ms = (timestamp - self._last_paint_ns) / 1_000_000.0
                    bucket["paint_gap_last_ms"] = gap_ms
                    bucket["paint_gap_max_ms"] = max(
                        bucket["paint_gap_max_ms"], gap_ms
                    )
            self._last_paint_ns = timestamp

    def snapshot(self, *, reset_interval: bool = False) -> dict[str, dict[str, int | float]]:
        """Return cumulative and interval counters.

        Resetting the interval bucket never changes cumulative totals or the
        last-paint clock, so an interval crossing a sample boundary remains
        measurable without retaining historical samples.
        """
        with self._lock:
            total = {key: self._total[key] for key in _FIELDS}
            interval = {key: self._interval[key] for key in _FIELDS}
            if reset_interval:
                self._interval = _empty()
            return {"total": total, "interval": interval}


__all__ = ["RenderTelemetry"]
