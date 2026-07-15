"""
Hyperwall — opt-in GUI-responsiveness tracer (HYPERWALL_PERFTRACE=1).

Two instruments, both logging through the normal app log:

- LoopLagSampler: a 25ms repeating QTimer on the GUI thread. Drift beyond
  the interval is time the main thread could not run (blocked in a slot,
  starved by paint, etc.). Logs a rolling summary every 10s and an
  immediate warning for any single stall > 100ms.

- @traced("name"): wraps a slot/handler; logs when one call takes > 25ms.
  Zero-cost passthrough when tracing is disabled.

Diagnosis findings that motivated this (2026-07-13): headless measurement
cleared the observer/callback layer (8 players ≈ 900 property callbacks/s
add no measurable event-loop lag), so "the wall feels less snappy" must be
caught in the real rendering environment — which only a live session sees.
"""

from __future__ import annotations

import functools
import logging
import os
import time
from typing import Any, Callable

from PyQt6.QtCore import QObject, QTimer

logger = logging.getLogger("HyperWall")

PERFTRACE_ENABLED = os.environ.get("HYPERWALL_PERFTRACE") == "1"

_TICK_MS = 25
_SUMMARY_S = 10
_STALL_WARN_MS = 100.0
_SLOW_SLOT_MS = 25.0


class LoopLagSampler(QObject):
    """Samples GUI event-loop lag; owned by the app once, GUI thread only."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lags: list[float] = []
        self._last = time.perf_counter()
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._summary = QTimer(self)
        self._summary.setInterval(_SUMMARY_S * 1000)
        self._summary.timeout.connect(self._log_summary)

    def start(self) -> None:
        self._last = time.perf_counter()
        self._timer.start()
        self._summary.start()
        logger.info("PERF trace on: loop sampler %dms, slow-slot > %dms.",
                    _TICK_MS, int(_SLOW_SLOT_MS))

    def _tick(self) -> None:
        now = time.perf_counter()
        lag = max(0.0, (now - self._last) * 1000 - _TICK_MS)
        self._last = now
        self._lags.append(lag)
        if lag > _STALL_WARN_MS:
            logger.warning("PERF loop stall: main thread blocked ~%.0fms", lag)

    def _log_summary(self) -> None:
        if not self._lags:
            return
        xs = sorted(self._lags)
        self._lags = []
        n = len(xs)
        mean = sum(xs) / n
        p95 = xs[min(n - 1, int(n * 0.95))]
        p99 = xs[min(n - 1, int(n * 0.99))]
        logger.info(
            "PERF loop-lag ms: mean %.1f  p95 %.1f  p99 %.1f  max %.1f  (n=%d)",
            mean, p95, p99, xs[-1], n,
        )


def traced(name: str) -> Callable:
    """Decorator: log calls slower than _SLOW_SLOT_MS. No-op when disabled."""
    def deco(fn: Callable) -> Callable:
        if not PERFTRACE_ENABLED:
            return fn

        # Qt signals append payload args (clicked emits a `checked` bool);
        # PyQt6 normally drops extras by inspecting the slot's arity, but a
        # bare *args wrapper defeats that inspection and the payload gets
        # through — under tracing, EVERY traced click handler crashed with
        # TypeError and the state machine drifted (caught live by the soak
        # exerciser's invariant checks, 2026-07-14). Cap positional args to
        # the wrapped function's true arity.
        max_pos = fn.__code__.co_argcount

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                return fn(*args[:max_pos], **kwargs)
            finally:
                dt = (time.perf_counter() - t0) * 1000
                if dt > _SLOW_SLOT_MS:
                    logger.warning("PERF slow slot %s: %.0fms", name, dt)
        return wrapper
    return deco
