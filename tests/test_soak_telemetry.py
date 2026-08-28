"""Contract tests for macOS soak-telemetry configuration.

These tests deliberately run without PyQt/libmpv.  They pin the measurement
contract that the live M5 soak must emit: periodic resource snapshots with a
platform-accurate RSS unit, an explicit audio-churn profile, and a
machine-readable session manifest that ties logs/stats back to the run.
"""
import ast
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _literal_assignments(path: str) -> dict[str, object]:
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    out: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    try:
                        out[target.id] = ast.literal_eval(node.value)
                    except ValueError:
                        pass
    return out


def test_soak_supports_audio_focused_profile():
    source = open(
        os.path.join(os.path.dirname(__file__), "..", "hyperwall", "soak.py"),
        encoding="utf-8",
    ).read()
    assert "HYPERWALL_SOAK_PROFILE" in source
    assert "audio" in source
    assert "_AUDIO_ACTIONS" in source


def test_soak_emits_machine_readable_manifest():
    source = open(
        os.path.join(os.path.dirname(__file__), "..", "hyperwall", "soak.py"),
        encoding="utf-8",
    ).read()
    assert "HYPERWALL_SOAK_REPORT_DIR" in source
    assert "hyperwall_soak_" in source
    assert 'self._write_report("start"' in source
    assert 'self._write_report(\n            "sample"' in source
    assert 'self._write_report(\n            "finish"' in source
    assert "root.mkdir(parents=True, exist_ok=True, mode=0o700)" in source
    assert "path.chmod(0o600)" in source


def test_soak_resource_metric_identifies_posix_peak_rss():
    source = open(
        os.path.join(os.path.dirname(__file__), "..", "hyperwall", "soak.py"),
        encoding="utf-8",
    ).read()
    assert "ws_metric" in source
    assert "peak_rss_mb" in source
    assert "current_ws_mb" in source
    assert "resident_rss_mb" in source


def test_render_telemetry_reports_interval_and_cumulative_values():
    from hyperwall.render_telemetry import RenderTelemetry

    telemetry = RenderTelemetry()
    telemetry.record_frame_ready()
    telemetry.record_frame_ready()
    telemetry.record_paint(
        paint_ms=4.0, render_ms=2.5, rendered=True, now_ns=1_000_000_000
    )
    telemetry.record_frame_ready()
    telemetry.record_paint(
        paint_ms=6.0, render_ms=3.5, rendered=False, now_ns=1_020_000_000
    )

    snapshot = telemetry.snapshot()
    assert snapshot["total"] == {
        "frame_ready": 3,
        "paint_calls": 2,
        "render_calls": 1,
        "render_errors": 1,
        "paint_total_ms": 10.0,
        "paint_max_ms": 6.0,
        "render_total_ms": 6.0,
        "render_max_ms": 3.5,
        "paint_gap_max_ms": 20.0,
        "paint_gap_last_ms": 20.0,
    }
    assert snapshot["interval"] == snapshot["total"]

    reset = telemetry.snapshot(reset_interval=True)
    assert reset["total"] == snapshot["total"]
    assert reset["interval"] == snapshot["interval"]
    assert telemetry.snapshot()["interval"] == {
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


def test_pre_mpv_paint_is_not_recorded_as_render_error():
    from hyperwall.render_telemetry import RenderTelemetry

    telemetry = RenderTelemetry()
    telemetry.record_paint(
        paint_ms=1.0,
        render_ms=0.0,
        rendered=False,
        render_attempted=False,
        now_ns=1_000_000_000,
    )
    snapshot = telemetry.snapshot()
    assert snapshot["total"]["paint_calls"] == 1
    assert snapshot["total"]["render_calls"] == 0
    assert snapshot["total"]["render_errors"] == 0


def test_render_telemetry_is_exported_with_audio_state_and_interval_samples():
    cell_source = open(
        os.path.join(os.path.dirname(__file__), "..", "hyperwall", "cell.py"),
        encoding="utf-8",
    ).read()
    wall_source = open(
        os.path.join(os.path.dirname(__file__), "..", "hyperwall", "wall.py"),
        encoding="utf-8",
    ).read()
    soak_source = open(
        os.path.join(os.path.dirname(__file__), "..", "hyperwall", "soak.py"),
        encoding="utf-8",
    ).read()
    assert "def telemetry_snapshot" in cell_source
    assert '"audio"' in cell_source
    assert "record_frame_ready" in open(
        os.path.join(os.path.dirname(__file__), "..", "hyperwall", "macembed.py"),
        encoding="utf-8",
    ).read()
    assert "record_paint" in open(
        os.path.join(os.path.dirname(__file__), "..", "hyperwall", "macembed.py"),
        encoding="utf-8",
    ).read()
    assert "render_telemetry" in wall_source
    assert "soak_telemetry_snapshot" in soak_source


def test_stats_summary_projects_render_telemetry_and_audio_state():
    from hyperwall.diagnostics import _stats_summary

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory, "stats.json")
        path.write_text(
            json.dumps(
                {
                    "n_cells": 1,
                    "cells": [
                        {
                            "cell": 0,
                            "playback_plan": {"server_mode": "direct"},
                            "render_telemetry": {
                                "frame_ready": 12,
                                "paint_calls": 10,
                                "render_calls": 9,
                                "render_errors": 1,
                                "paint_total_ms": 20.0,
                                "paint_max_ms": 4.0,
                                "render_total_ms": 15.0,
                                "render_max_ms": 3.0,
                                "paint_gap_max_ms": 33.0,
                                "paint_gap_last_ms": 16.0,
                            },
                            "audio_state": {
                                "muted": True,
                                "audio_started": False,
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        summary = _stats_summary(path)

    assert summary["render_telemetry"][0]["cell"] == 0
    assert summary["render_telemetry"][0]["render_errors"] == 1
    assert summary["audio_state"] == [
        {"cell": 0, "muted": True, "audio_started": False}
    ]


def test_macos_soak_launcher_collects_system_telemetry():
    path = os.path.join(os.path.dirname(__file__), "..", "soak_wall.sh")
    source = open(path, encoding="utf-8").read()
    for expected in (
        "HYPERWALL_SOAK_PROFILE=audio",
        "HYPERWALL_SOAK_REPORT_DIR",
        "HYPERWALL_STATS=1",
        "HYPERWALL_PERFTRACE=1",
        "powermetrics",
        "nettop",
        "vm_stat",
        "chmod 700",
        "datetime.now(timezone.utc)",
    ):
        assert expected in source


def test_soak_launcher_honors_runner_report_directory():
    path = os.path.join(os.path.dirname(__file__), "..", "soak_wall.sh")
    source = open(path, encoding="utf-8").read()
    assert 'HYPERWALL_SOAK_REPORT_DIR:-$REPORT_ROOT/$RUN_ID' in source


def test_normal_launcher_clears_stale_soak_environment():
    path = os.path.join(os.path.dirname(__file__), "..", "launch.sh")
    source = open(path, encoding="utf-8").read()
    assert 'HYPERWALL_SOAK_ACTIVE:-0' in source
    assert 'unset HYPERWALL_SOAK_MINUTES' in source
    assert 'unset HYPERWALL_SOAK_REPORT_DIR' in source
    assert 'unset HYPERWALL_NO_LOG_SETUP' in source


def test_soak_launcher_marks_explicit_soak_mode():
    path = os.path.join(os.path.dirname(__file__), "..", "soak_wall.sh")
    source = open(path, encoding="utf-8").read()
    assert 'HYPERWALL_SOAK_ACTIVE=1' in source


def test_macos_soak_prevents_idle_sleep_during_measurement():
    path = os.path.join(os.path.dirname(__file__), "..", "soak_wall.sh")
    source = open(path, encoding="utf-8").read()
    assert "command -v caffeinate" in source
    assert "caffeinate -dims" in source


def run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS  {test.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {test.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
