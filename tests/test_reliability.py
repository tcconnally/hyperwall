"""
Unit tests for hyperwall.reliability (Epic 2) and the constants that drive it.

Pure logic only — no PyQt / mpv / Emby. Runnable anywhere Python is.
Run: python tests/test_reliability.py
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from hyperwall.reliability import (  # noqa: E402
    count_recent,
    escalation_plan,
    is_stalled,
    scale_demuxer_mb,
    should_park,
)


# ── is_stalled ────────────────────────────────────────────────────────────────

def test_stall_fires_after_threshold():
    assert is_stalled(25.0, paused=False, dragging=False, threshold_s=20)


def test_no_stall_within_threshold():
    assert not is_stalled(5.0, paused=False, dragging=False, threshold_s=20)


def test_paused_never_stalls():
    # A paused cell isn't making progress on purpose — must not be flagged.
    assert not is_stalled(999.0, paused=True, dragging=False, threshold_s=20)


def test_seeking_never_stalls():
    assert not is_stalled(999.0, paused=False, dragging=True, threshold_s=20)


def test_stall_boundary_is_strict():
    # Exactly at threshold is NOT stalled (must exceed).
    assert not is_stalled(20.0, paused=False, dragging=False, threshold_s=20)
    assert is_stalled(20.001, paused=False, dragging=False, threshold_s=20)


# ── count_recent / should_park ────────────────────────────────────────────────

def test_count_recent_window():
    times = [0.0, 10.0, 55.0, 58.0, 59.5]
    # now=60, window=60 → 0.0 is 60s ago (inclusive), all 5 count
    assert count_recent(times, now=60.0, window_s=60.0) == 5
    # window=10 → only 55,58,59.5 within last 10s
    assert count_recent(times, now=60.0, window_s=10.0) == 3


def test_should_park_triggers_on_threshold():
    times = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert should_park(times, now=6.0, window_s=60, threshold=5)


def test_should_not_park_below_threshold():
    times = [1.0, 2.0, 3.0]
    assert not should_park(times, now=6.0, window_s=60, threshold=5)


def test_old_failures_age_out_of_window():
    # Four ancient failures + one recent → not a loop.
    times = [1.0, 2.0, 3.0, 4.0, 500.0]
    assert not should_park(times, now=505.0, window_s=60, threshold=5)


# ── scale_demuxer_mb ──────────────────────────────────────────────────────────

def test_single_cell_gets_full_per_cell():
    assert scale_demuxer_mb(1, per_cell_mb=512, total_budget_mb=3072) == 512


def test_small_grid_under_budget_unscaled():
    # 4 cells * 512 = 2048 <= 3072 → no scaling.
    assert scale_demuxer_mb(4, per_cell_mb=512, total_budget_mb=3072) == 512


def test_large_grid_scaled_down():
    # 36 cells, 3072 budget → 3072/36 = 85 MiB each (well under 512).
    got = scale_demuxer_mb(36, per_cell_mb=512, total_budget_mb=3072)
    assert got == 85, got
    assert got * 36 <= 3072


def test_floor_respected():
    # Absurd cell count clamps to the floor, never zero.
    got = scale_demuxer_mb(10_000, per_cell_mb=512, total_budget_mb=3072, floor_mb=32)
    assert got == 32


def test_zero_cells_is_safe():
    # Defensive: n<1 must not divide-by-zero.
    assert scale_demuxer_mb(0, per_cell_mb=512, total_budget_mb=3072) == 512


# ── escalation_plan (retry → transcode → skip) ────────────────────────────────

def test_attempt1_retries_direct():
    p = escalation_plan(1, max_retries=3)
    assert p["action"] == "retry"
    assert p["transcode"] is False   # first attempt stays DIRECT
    assert p["delay_s"] == 2         # 2**1


def test_attempt2_escalates_to_transcode():
    p = escalation_plan(2, max_retries=3)
    assert p["action"] == "retry"
    assert p["transcode"] is True    # escalate at attempt >= 2
    assert p["delay_s"] == 4         # 2**2


def test_attempt3_still_retries_transcode():
    p = escalation_plan(3, max_retries=3)
    assert p["action"] == "retry"
    assert p["transcode"] is True
    assert p["delay_s"] == 8         # 2**3


def test_attempt_over_max_skips():
    p = escalation_plan(4, max_retries=3)
    assert p["action"] == "skip"
    assert p["transcode"] is False
    assert p["delay_s"] == 0


def test_full_escalation_sequence():
    # The exact retry→transcode→skip ladder a dead stream walks.
    seq = [escalation_plan(a, 3) for a in range(1, 5)]
    actions = [p["action"] for p in seq]
    transcodes = [p["transcode"] for p in seq]
    assert actions == ["retry", "retry", "retry", "skip"]
    assert transcodes == [False, True, True, False]


# ── constants env clamping (integration of _int_env) ──────────────────────────

def test_constants_defaults_load():
    from hyperwall import constants as c
    assert c.STALL_TIMEOUT_S == 20
    assert c.WATCHDOG_INTERVAL_MS == 5_000
    assert c.CRASH_LOOP_THRESHOLD == 5
    assert c.DEMUXER_PER_CELL_MB == 512
    assert c.CACHE_BUDGET_MB == 3_072


def test_apply_cache_budget_shape():
    from hyperwall.constants import apply_cache_budget
    out = apply_cache_budget({"demuxer_max_bytes": "512MiB", "vo": "gpu-next"}, 36)
    assert out["vo"] == "gpu-next"           # untouched keys preserved
    assert out["demuxer_max_bytes"].endswith("MiB")
    assert int(out["demuxer_max_bytes"][:-3]) < 512  # scaled down for 36 cells


def run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    sys.exit(run_all())
