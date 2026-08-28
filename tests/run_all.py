"""
Run every Hyperwall test suite and aggregate the result.

Single entry point for CI and local dev — no pytest dependency. Discovers the
sibling test modules, runs each module's run_all(), and returns nonzero if any
suite has a failure.

Run: python tests/run_all.py
"""

from __future__ import annotations

import importlib
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SUITES = [
    "run_repo_guards",
    "test_reliability",
    "test_playback_plan",
    "test_playback_state",
    "test_session_broker",
    "test_urls",
    "test_config",
    "test_preview_displays",
    "test_display_identity",
    "test_diagnostics",
    "test_sync",
    "test_playlist",
    "test_scenes",
    "test_backends",
    "test_theme",
    "test_instrumentation",
    "test_mute_volume",
    "test_audit_regressions",
    "test_freeze_visibility",
    "test_platform",
    "test_soak_telemetry",
    "test_frame_pump",
    "test_frame_pump_integration",
    "test_frame_pump_telemetry",
    "test_performance_roadmap",
    "test_macos_profile",
    "test_macos_playback_performance",
    "test_endpoint_override",
]


def main() -> int:
    total_failures = 0
    for name in SUITES:
        print(f"\n=== {name} ===")
        mod = importlib.import_module(name)
        total_failures += mod.run_all()
    print("\n" + "=" * 48)
    if total_failures:
        print(f"OVERALL: FAIL ({total_failures} failing test(s))")
    else:
        print("OVERALL: PASS (all suites green)")
    return total_failures


if __name__ == "__main__":
    sys.exit(main())
