"""Tests for sanitized macOS native-profile parsing."""
from __future__ import annotations

import os
import sys
from pathlib import Path

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
