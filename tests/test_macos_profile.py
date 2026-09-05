"""Tests for sanitized macOS native-profile parsing."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(_ROOT))


_POWERMETRICS = """\
*** Sampled system activity (Fri Aug 28 17:29:37 2026 -0500) (1000ms elapsed) ***
*** Running tasks ***
Name                               ID     CPU ms/s  User%  Deadlines (<2 ms, 2-5 ms)  Wakeups (Intr, Pkg idle)
Python                             80232  2141.43  84.50  0.00    0.00               100.00 20.00
WindowServer                       596    129.16   62.00  0.00    0.00               20.00  10.00
ALL_TASKS                          -2     3256.92  56.00  0.00    0.00               200.00 50.00
**** Thermal pressure ****
Current pressure level: Heavy
GPU idle residency: 8.11%
*** Sampled system activity (Fri Aug 28 17:29:38 2026 -0500) (1000ms elapsed) ***
*** Running tasks ***
Name                               ID     CPU ms/s  User%  Deadlines (<2 ms, 2-5 ms)  Wakeups (Intr, Pkg idle)
Python                             80232  2870.82  85.00  0.00    0.00               100.00 20.00
ALL_TASKS                          -2     4398.01  60.00  0.00    0.00               200.00 50.00
**** Thermal pressure ****
Current pressure level: Heavy
GPU idle residency: 3.22%
"""

_SAMPLE = """\
Thread 0x1:
    12  libsystem_kernel.dylib     mach_msg_trap
    11  libmpv.2.dylib             mpv_render_context_render
    10  libavcodec.dylib            avcodec_send_packet
Thread 0x2:
    8   Python                     _PyEval_EvalFrameDefault
    7   QtGui                      QOpenGLWidget::paintGL
"""


def test_powermetrics_parser_aggregates_target_process_and_thermal_state():
    from hyperwall.macos_profile import parse_powermetrics

    report = parse_powermetrics(_POWERMETRICS, process_name="Python", process_pid=80232)

    assert report["sample_count"] == 2
    assert report["process"]["pid"] == 80232
    assert report["process"]["samples"] == 2
    assert report["process"]["cpu_ms_s"]["mean"] == 2506.125
    assert report["process"]["cpu_ms_s"]["max"] == 2870.82
    assert report["thermal_levels"] == {"Heavy": 2}
    assert report["gpu_idle_residency"]["mean"] == 5.665


def test_powermetrics_parser_marks_permission_failure_incomplete():
    from hyperwall.macos_profile import parse_powermetrics

    report = parse_powermetrics("powermetrics must be invoked as the superuser")

    assert report["sample_count"] == 0
    assert report["complete"] is False
    assert "permission_denied" in report["missing_evidence"]


def test_sample_parser_counts_native_hot_stack_labels():
    from hyperwall.macos_profile import parse_sample_stacks

    report = parse_sample_stacks(_SAMPLE)

    assert report["complete"] is True
    assert report["thread_count"] == 2
    assert report["labels"]["mpv_render_context_render"] == 1
    assert report["labels"]["avcodec_send_packet"] == 1
    assert report["labels"]["QOpenGLWidget::paintGL"] == 1
    assert report["labels"]["_PyEval_EvalFrameDefault"] == 1


def test_sample_parser_marks_empty_capture_incomplete():
    from hyperwall.macos_profile import parse_sample_stacks

    report = parse_sample_stacks("")

    assert report["complete"] is False
    assert "sample_missing" in report["missing_evidence"]


def test_profile_cli_selects_highest_passing_capacity_mode():
    profiles = [
        {
            "cell_count": 4,
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
        },
        {
            "cell_count": 6,
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
        },
        {
            "cell_count": 8,
            "duration_coverage": 1.0,
            "p95_loop_lag_ms": 10.0,
            "max_render_gap_ms": 40.0,
            "cpu_cores_mean": 2.0,
            "loop_stalls_ge_100ms": 1,
            "freeze_count": 0,
            "decoder_faults": 0,
            "audio_underruns": 0,
            "av_desync": 0,
            "transport_errors": 0,
            "power_sleep_evidence": 1,
        },
    ]
    with tempfile.TemporaryDirectory() as directory:
        paths = []
        for index, profile in enumerate(profiles):
            path = Path(directory, f"profile-{index}.json")
            path.write_text(json.dumps(profile), encoding="utf-8")
            paths.append(str(path))
        result = subprocess.run(
            [sys.executable, str(_ROOT / "scripts" / "profile-macos-render.py"), "--matrix", *paths],
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["capacity"]["status"] == "PASS"
    assert report["capacity"]["selected_cells"] == 6


def test_profile_cli_accepts_analyze_run_reports():
    analysis = {
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
        "native_profile": {
            "powermetrics": {"process": {"cpu_ms_s": {"mean": 2.14}}},
        },
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory, "analysis.json")
        path.write_text(json.dumps(analysis), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(_ROOT / "scripts" / "profile-macos-render.py"), "--matrix", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["capacity"]["selected_cells"] == 6
    assert report["profiles"][0]["cpu_cores_mean"] == 2.14


def test_profile_cli_blocks_when_capacity_matrix_has_no_passing_mode():
    profile = {
        "cell_count": 8,
        "duration_coverage": 1.0,
        "p95_loop_lag_ms": 30.0,
        "max_render_gap_ms": 40.0,
        "cpu_cores_mean": 2.0,
        "loop_stalls_ge_100ms": 1,
        "freeze_count": 0,
        "decoder_faults": 0,
        "audio_underruns": 0,
        "av_desync": 0,
        "transport_errors": 0,
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory, "profile.json")
        path.write_text(json.dumps(profile), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(_ROOT / "scripts" / "profile-macos-render.py"), "--matrix", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )

    assert result.returncode == 2
    report = json.loads(result.stdout)
    assert report["capacity"]["status"] == "BLOCK"
    assert report["capacity"]["selected_cells"] is None


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
