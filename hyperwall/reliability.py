"""
Hyperwall — pure reliability helpers (no PyQt / mpv / Emby imports).

The 24/7-wall self-healing logic lives here as small, side-effect-free
functions so it can be unit-tested without a display server, an mpv build, or
a live Emby instance. `cell.py` imports these; the wiring (QTimers, mpv calls,
overlays) stays in the widget while the *decisions* are verifiable in isolation.

Six concerns (Epic 2 / #7 + the 2026-07-11 stampede/lockup reviews):
  - stall detection      → is_stalled()
  - crash-loop parking   → count_recent() / should_park()
  - cache-budget scaling → scale_demuxer_mb()
  - retry desync         → apply_jitter()
  - outage detection     → is_systemic_outage()
  - mpv event decoding   → end_file_reason()
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


def scale_readahead_s(n_cells: int, base_s: int = 60, floor_s: int = 10) -> int:
    """Per-cell demuxer readahead (seconds), scaled down as cells go up.

    Readahead depth is burst SIZE: on every track open, mpv fills it at
    line rate. Eight cells each slurping 60s of a 20 Mbps stream produce
    fill-bursts that starve the other cells' steady reads — measured
    2026-07-14: 80% of freeze episodes began within 8s of a stream-open.
    <=4 cells keep the full depth (measured clean); beyond that the depth
    shrinks so aggregate burst demand stays roughly constant.
    """
    n = max(1, int(n_cells))
    if n <= 4:
        return base_s
    return max(floor_s, int(base_s * 4 / n))


def scale_bitrate_budget_mbps(
    n_cells: int, base_mbps: int, link_mbps: int = 800,
) -> int:
    """Cell-count-aware direct-play bitrate cap (the graduated middle
    ground between transcode-everything and direct-everything).

    Items above the cap transcode server-side. The cap divides the usable
    link (`link_mbps`) across cells with 2x burst headroom; at <=4 cells it
    resolves to the configured base (measured clean), at 8 cells ~50 Mbps so
    only genuinely heavy 50-60+ Mbps outliers transcode while the bulk of the
    library stays direct.

    The headroom was relaxed 3x->2x (33->50 Mbps at 8 cells, 2026-07-15):
    the original 33 sent ~6 of 8 startup cells to transcode at once and
    stampeded greg's media engine (HTTP 500s + partial, pixelated segments).
    Fewer files now transcode, and a concurrency gate (see gate_auto_transcode)
    caps how many run at once — direct-play is the reliable path on this setup.

    `link_mbps` defaults to 800 (greg->skyhawk 1 GbE usable goodput);
    callers pass constants.LINK_MBPS so it retunes with the real link.
    """
    n = max(1, int(n_cells))
    return min(base_mbps, max(8, link_mbps // (2 * n)))


def gate_auto_transcode(
    want_auto: bool, active_transcodes: int, max_concurrent: int,
) -> bool:
    """Whether an AUTO transcode should proceed given how many cells are
    already transcoding.

    Forced retries (a file that failed direct play and MUST transcode) bypass
    this in the caller. Protects greg's Arc A310 / QuickSync media engine from
    a cold-start stampede — 8 cells escalating at once produced HTTP 500s and
    partial, pixelated HLS segments (2026-07-15). Over the cap, the heavy clip
    direct-plays instead (reliable). max_concurrent <= 0 disables the gate.
    """
    if not want_auto:
        return False
    if max_concurrent <= 0:
        return True
    return active_transcodes < max_concurrent


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


# libmpv MPV_END_FILE_REASON_* values, as surfaced by python-mpv's
# MpvEventEndFile.reason when only the int is available.
_END_FILE_REASONS = {
    0: "eof",
    1: "restarted",
    2: "stop",
    3: "quit",
    4: "error",
    5: "redirect",
}


def end_file_reason(ev: object) -> str:
    """Extract the canonical end-file reason string from a python-mpv event.

    Duck-typed across python-mpv API generations (probed live 2026-07-12
    against the shipped mpv-2.dll + python-mpv 1.x):
      - 1.x: ev.as_dict() → {"reason": b"stop"/b"error"/...} (bytes!)
      - 1.x: ev.data → MpvEventEndFile with .reason as an int
      - legacy: ev.event → plain dict with a "reason" key

    The old inline extraction did `ev.event.get("reason")`, which raises
    AttributeError on python-mpv 1.x and silently defaulted EVERY event —
    including load errors and loadfile replaces — to "eof". Unknown shapes
    still fall back to "eof" (the historic default) so a future API change
    degrades to the old behavior rather than crashing the event thread.
    """
    def _norm(raw: object) -> str | None:
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        if isinstance(raw, bool):  # bools are ints; reject explicitly
            return None
        if isinstance(raw, int):
            return _END_FILE_REASONS.get(raw, "eof")
        if isinstance(raw, str) and raw:
            return raw
        return None

    try:
        got = _norm(ev.as_dict().get("reason"))  # type: ignore[attr-defined]
        if got is not None:
            return got
    except Exception:
        pass
    try:
        got = _norm(getattr(getattr(ev, "data", None), "reason", None))
        if got is not None:
            return got
    except Exception:
        pass
    try:
        legacy = getattr(ev, "event", None)
        if isinstance(legacy, dict):
            got = _norm(legacy.get("reason"))
            if got is not None:
                return got
    except Exception:
        pass
    return "eof"


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
