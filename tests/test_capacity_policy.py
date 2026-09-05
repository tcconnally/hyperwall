"""Tests for the fail-closed M5 cell-capacity policy."""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(_ROOT))


def _profile(cells: int, **overrides):
    result = {
        "cell_count": cells,
        "duration_coverage": 1.0,
        "p95_loop_lag_ms": 10.0,
        "max_render_gap_ms": 40.0,
        "cpu_cores_mean": 2.0,
        "loop_stalls_ge_100ms": 0,
        "freeze_count": 0,
        "decoder_faults": 0,
        "audio_underruns": 0,
        "av_desync": 0,
        "transport_errors": 0,
        "power_sleep_evidence": 1,
    }
    result.update(overrides)
    return result


def test_analysis_report_is_normalized_for_capacity_selection():
    from hyperwall.capacity_policy import capacity_profile_from_analysis, select_capacity

    report = {
        "stats": {
            "n_cells": 6,
            "render_telemetry": [{"paint_gap_max_ms": 80.0}],
        },
        "log": {
            "p95_loop_lag_ms": 20.0,
            "loop_stalls_ge_100ms": 0,
            "freeze_count": 0,
            "hardware_decode_failures": 0,
            "decoder_buffer_warnings": 0,
            "video_decode_errors": 0,
            "audio_decode_errors": 0,
            "audio_underrun": 0,
            "av_desync": 0,
            "connection_refused": 0,
            "hls_segment_failures": 0,
            "stream_open_failures": 0,
            "playback_errors": 0,
            "retry_skips": 0,
        },
        "gates": {
            "duration_coverage": {"value": {"coverage": 1.0}},
            "power_sleep_evidence": {"status": "PASS"},
        },
    }
    native = {
        "powermetrics": {
            "process": {"cpu_ms_s": {"mean": 2.14}},
        },
    }

    profile = capacity_profile_from_analysis(report, native_profile=native)

    assert profile["cell_count"] == 6
    assert profile["duration_coverage"] == 1.0
    assert profile["p95_loop_lag_ms"] == 20.0
    assert profile["max_render_gap_ms"] == 80.0
    assert profile["cpu_cores_mean"] == 2.14
    assert profile["decoder_faults"] == 0
    assert select_capacity([profile])["status"] == "PASS"


def test_power_sleep_gate_blocks_capacity_promotion_when_incomplete():
    from hyperwall.capacity_policy import capacity_profile_from_analysis, select_capacity

    report = {
        "stats": {
            "n_cells": 8,
            "render_telemetry": [{"paint_gap_max_ms": 40.0}],
        },
        "log": {
            "p95_loop_lag_ms": 10.0,
            "loop_stalls_ge_100ms": 0,
            "freeze_count": 0,
            "hardware_decode_failures": 0,
            "decoder_buffer_warnings": 0,
            "video_decode_errors": 0,
            "audio_decode_errors": 0,
            "audio_underrun": 0,
            "av_desync": 0,
            "connection_refused": 0,
            "hls_segment_failures": 0,
            "stream_open_failures": 0,
            "playback_errors": 0,
            "retry_skips": 0,
        },
        "gates": {
            "duration_coverage": {"status": "PASS", "value": {"coverage": 1.0}},
            "power_sleep_evidence": {
                "status": "BLOCK",
                "value": {"coverage_ok": False},
            },
        },
        "verdict": "BLOCK",
    }
    native = {
        "powermetrics": {
            "process": {"cpu_ms_s": {"mean": 2.0}},
        },
    }

    profile = capacity_profile_from_analysis(report, native_profile=native)
    decision = select_capacity([profile])

    assert decision["status"] == "BLOCK"
    assert decision["selected_cells"] is None
    assert "power_sleep_evidence" in decision["candidates"][0]["missing"]


def test_highest_passing_cell_count_is_selected():
    from hyperwall.capacity_policy import select_capacity

    decision = select_capacity([
        _profile(4),
        _profile(6),
        _profile(8, freeze_count=1),
    ])

    assert decision["status"] == "PASS"
    assert decision["selected_cells"] == 6
    assert decision["first_failing_cells"] == [8]


def test_responsiveness_thresholds_block_a_candidate():
    from hyperwall.capacity_policy import select_capacity

    decision = select_capacity([_profile(4, p95_loop_lag_ms=25.1)])

    assert decision["status"] == "BLOCK"
    assert decision["selected_cells"] is None
    assert "p95_loop_lag_ms" in decision["candidates"][0]["failures"]


def test_missing_metrics_fail_closed_for_a_candidate():
    from hyperwall.capacity_policy import select_capacity

    decision = select_capacity([{"cell_count": 4, "duration_coverage": 1.0}])

    assert decision["status"] == "BLOCK"
    assert decision["selected_cells"] is None
    assert decision["candidates"][0]["status"] == "BLOCK"
    assert "loop_stalls_ge_100ms" in decision["candidates"][0]["missing"]


def test_no_passing_mode_does_not_fallback_to_four_cells():
    from hyperwall.capacity_policy import select_capacity

    decision = select_capacity([
        _profile(4, loop_stalls_ge_100ms=1),
        _profile(6, freeze_count=1),
        _profile(8, decoder_faults=1),
    ])

    assert decision["status"] == "BLOCK"
    assert decision["selected_cells"] is None
    assert decision["first_failing_cells"] == [4, 6, 8]


def test_invalid_or_duplicate_cell_counts_are_rejected():
    from hyperwall.capacity_policy import select_capacity

    decision = select_capacity([_profile(4), _profile(4), _profile(5)])

    assert decision["status"] == "BLOCK"
    assert decision["selected_cells"] is None
    assert decision["invalid_profiles"] == [5]
    assert decision["duplicate_cells"] == [4]


def run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS  {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"  {passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(run_all())
