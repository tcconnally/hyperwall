"""
Hyperwall — pure reliability helpers (no PyQt / mpv / Emby imports).

The 24/7-wall self-healing logic lives here as small, side-effect-free
functions so it can be unit-tested without a display server, an mpv build, or
a live Emby instance. `cell.py` imports these; the wiring (QTimers, mpv calls,
overlays) stays in the widget while the *decisions* are verifiable in isolation.

Five concerns (Epic 2 / #7 + the 2026-07-11 stampede review):
  - stall detection      → is_stalled()
  - crash-loop parking   → count_recent() / should_park()
  - cache-budget scaling → scale_demuxer_mb()
  - retry desync         → apply_jitter()
  - outage detection     → is_systemic_outage()
"""

from __future__ import annotations

from typing import Iterable


def is_stalled(
    idle_s: float,
    *,
    paused: bool,
    dragging: bool,
    threshold_s: float,
) -> bool:
    """Decide whether a cell has silently stalled (frozen mid-stream).

    A stall is "no forward time-pos progress for longer than threshold_s while
    the cell is actively trying to play." Paused/seeking cells never count —
    their lack of progress is intentional, not a fault.
    """
    if paused or dragging:
        return False
    return idle_s > threshold_s


def count_recent(times: Iterable[float], now: float, window_s: float) -> int:
    """Count timestamps within the trailing window ending at `now`."""
    return sum(1 for t in times if 0 <= now - t <= window_s)


def should_park(
    times: Iterable[float],
    now: float,
    *,
    window_s: float,
    threshold: int,
) -> bool:
    """True when failures within the rolling window reach the park threshold.

    Distinguishes a single bad item (skip and move on) from a systemic outage
    (e.g. Emby down) where a cell would otherwise hammer the server forever.
    """
    return count_recent(times, now, window_s) >= threshold


def scale_demuxer_mb(
    n_cells: int,
    *,
    per_cell_mb: int,
    total_budget_mb: int,
    floor_mb: int = 32,
) -> int:
    """Per-cell demuxer budget (MiB), scaled so the grid total stays bounded.

    Each cell wants `per_cell_mb`, but N cells must not exceed
    `total_budget_mb` in aggregate (a 6x6 grid at 512 MiB/cell would reach
    ~18 GB). Returns min(per_cell_mb, budget/N) clamped to `floor_mb`.
    """
    n = max(1, int(n_cells))
    per = min(per_cell_mb, total_budget_mb / n)
    return int(max(floor_mb, per))


def apply_jitter(delay_s: float, rand: float) -> float:
    """Spread a retry delay over [0.75x, 1.25x] to desynchronize cells.

    Every cell computes the same escalation_plan delays, so a wall-wide fault
    (Emby hiccup, network stall) makes all cells retry at the same instant —
    a thundering herd against an already-struggling server. `rand` is an
    injected uniform sample in [0, 1) (pass random.random()) so this stays
    pure and deterministic under test.
    """
    r = min(1.0, max(0.0, float(rand)))
    return delay_s * (0.75 + 0.5 * r)


def is_systemic_outage(
    events: Iterable[tuple[float, object]],
    now: float,
    *,
    window_s: float,
    total_cells: int,
    min_cells: int = 3,
) -> bool:
    """True when enough *distinct* cells failed recently to imply a shared
    cause (server/network outage) rather than per-item bad media.

    `events` is an iterable of (timestamp, cell_key) failure records. The
    trigger is a majority of the wall, but never fewer than `min_cells`, so
    small walls (1–2 cells) keep plain per-cell escalation — with that few
    cells, "systemic vs bad file" is indistinguishable anyway.

    Callers use this to switch strategy: back off longer and *don't* escalate
    to server transcode (piling N concurrent transcode jobs onto a server
    that's already stalling makes the outage worse — observed 2026-07-11).
    """
    if total_cells < min_cells:
        return False
    recent = {
        key for t, key in events if 0 <= now - t <= window_s
    }
    needed = max(min_cells, (total_cells + 1) // 2)
    return len(recent) >= needed


def escalation_plan(attempt: int, max_retries: int) -> dict:
    """Decide what to do after a playback failure at 1-based `attempt`.

    Returns a small plan dict describing the retry/escalation policy, so the
    behavior is testable without QTimer/mpv:

      - action: "retry" (try again) or "skip" (give up, advance playlist)
      - transcode: True once attempt >= 2 (escalate to server transcode)
      - delay_s: backoff before the next attempt (2**attempt for retries, 0
                 for a skip)

    attempt 1 → retry direct (2s), attempt 2 → retry transcode (4s),
    attempt 3 → retry transcode (8s), attempt 4 (> max_retries=3) → skip.
    """
    if attempt <= max_retries:
        return {
            "action": "retry",
            "transcode": attempt >= 2,
            "delay_s": 2 ** attempt,
        }
    return {"action": "skip", "transcode": False, "delay_s": 0}
