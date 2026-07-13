"""
Hyperwall — opt-in soak/endurance harness (HYPERWALL_SOAK_MINUTES=N).

Turns a normal, manually-launched wall session into a fixed-length
randomized endurance run and self-terminates cleanly at the end (the
graceful shutdown path — stats dump included when HYPERWALL_STATS=1).

Three instruments:

- Churn driver: every HYPERWALL_SOAK_DWELL_S (default 75s, ±25% jitter,
  0 disables) a random cell advances, on top of natural EOF advances.
  This forces far more transitions per hour than natural playout —
  coverage of the advance/prefetch/retry machinery across the library.

- Resource sampler (every 60s): working set, private bytes, GDI and
  USER object counts, thread count. Leaks here are the classic
  long-running-wall killers (a GDI leak of a few objects per player
  reuse kills the process at the 10k default cap).

- End-of-run: logs a SOAK summary and triggers the normal shutdown.

Analysis happens offline from the log: SOAK res lines (leak slopes),
PERF lines (loop lag over time), [PREFETCH→]/[DIRECT]/stall/error lines
(advance health), and the hyperwall_stats_*.json dump (decode quality).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import random
import time

from PyQt6.QtCore import QObject, QTimer

logger = logging.getLogger("HyperWall")

SOAK_MINUTES = int(os.environ.get("HYPERWALL_SOAK_MINUTES", "0") or 0)
SOAK_DWELL_S = int(os.environ.get("HYPERWALL_SOAK_DWELL_S", "75") or 0)

_RES_SAMPLE_S = 60


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _resource_snapshot() -> dict[str, int]:
    """Working set / private bytes / GDI / USER / threads for this process."""
    out: dict[str, int] = {}
    try:
        k32 = ctypes.windll.kernel32
        u32 = ctypes.windll.user32
        psapi = ctypes.windll.psapi
        h = k32.GetCurrentProcess()
        pmc = _PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            out["ws_mb"] = pmc.WorkingSetSize // (1024 * 1024)
            out["private_mb"] = pmc.PagefileUsage // (1024 * 1024)
        out["gdi"] = u32.GetGuiResources(h, 0)   # GR_GDIOBJECTS
        out["user"] = u32.GetGuiResources(h, 1)  # GR_USEROBJECTS
    except Exception as e:
        logger.debug("SOAK resource snapshot failed: %s", e)
    try:
        import threading
        out["threads"] = threading.active_count()
    except Exception:
        pass
    return out


class SoakController(QObject):
    """Owns the churn, sampling, and end-of-run timers. GUI thread only."""

    def __init__(self, wall) -> None:
        super().__init__(wall)
        self._wall = wall
        self._t0 = time.monotonic()
        self._advances = 0
        self._baseline = _resource_snapshot()

        self._res_timer = QTimer(self)
        self._res_timer.setInterval(_RES_SAMPLE_S * 1000)
        self._res_timer.timeout.connect(self._sample)
        self._res_timer.start()

        self._churn_timer = QTimer(self)
        self._churn_timer.setSingleShot(True)
        if SOAK_DWELL_S > 0:
            self._churn_timer.timeout.connect(self._churn)
            self._arm_churn()

        self._end_timer = QTimer(self)
        self._end_timer.setSingleShot(True)
        self._end_timer.timeout.connect(self._finish)
        self._end_timer.start(SOAK_MINUTES * 60 * 1000)

        logger.info(
            "SOAK start: %d min, dwell %ds (0=natural only), baseline %s",
            SOAK_MINUTES, SOAK_DWELL_S, self._baseline,
        )

    def _arm_churn(self) -> None:
        # Per-cell dwell: with N cells, one advance per DWELL/N keeps each
        # cell's average dwell at DWELL. ±25% jitter desynchronizes.
        cells = max(1, len(self._wall.cells))
        base_ms = SOAK_DWELL_S * 1000 / cells
        self._churn_timer.start(int(base_ms * random.uniform(0.75, 1.25)))

    def _churn(self) -> None:
        try:
            cell = random.choice(self._wall.cells)
            self._advances += 1
            self._wall.next_video(cell, False)
        except Exception as e:
            logger.warning("SOAK churn advance failed: %s", e)
        self._arm_churn()

    def _sample(self) -> None:
        snap = _resource_snapshot()
        mins = (time.monotonic() - self._t0) / 60
        logger.info(
            "SOAK res @%.0fmin: ws=%sMB private=%sMB gdi=%s user=%s "
            "threads=%s churn_advances=%d",
            mins, snap.get("ws_mb"), snap.get("private_mb"),
            snap.get("gdi"), snap.get("user"), snap.get("threads"),
            self._advances,
        )

    def _finish(self) -> None:
        snap = _resource_snapshot()
        logger.info(
            "SOAK done after %d min: churn_advances=%d  baseline %s → final %s",
            SOAK_MINUTES, self._advances, self._baseline, snap,
        )
        self._res_timer.stop()
        self._churn_timer.stop()
        self._wall._shutdown()
