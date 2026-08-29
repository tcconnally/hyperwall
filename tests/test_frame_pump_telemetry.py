"""Tests for exported frame-pump counters."""
from __future__ import annotations

import os
from pathlib import Path
import sys


_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(_ROOT))


def test_mpv_snapshot_exports_frame_pump_counters():
    source = (_ROOT / "hyperwall" / "macembed.py").read_text(encoding="utf-8")
    assert "frame_pump" in source
    assert "self._frame_pump.snapshot()" in source
    assert "self._frame_pump.request()" in source
    assert "self._frame_pump.finish_paint()" in source


def test_stats_dump_exports_frame_pump_separately_from_render_counters():
    source = (_ROOT / "hyperwall" / "wall.py").read_text(encoding="utf-8")
    assert '"frame_pump": render.get("frame_pump", {})' in source


def test_stats_summary_projects_frame_pump_counters():
    from hyperwall.diagnostics import _stats_summary

    with __import__("tempfile").TemporaryDirectory() as directory:
        path = Path(directory, "stats.json")
        path.write_text(
            __import__("json").dumps(
                {
                    "n_cells": 1,
                    "cells": [
                        {
                            "cell": 0,
                            "totals": {},
                            "info": {},
                            "freezes": 0,
                            "freeze_seconds": 0,
                            "frame_pump": {
                                "callbacks": 100,
                                "queued_updates": 25,
                                "coalesced_callbacks": 75,
                                "ignored_callbacks": 0,
                                "pending": False,
                                "closed": True,
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        summary = _stats_summary(path)

    assert summary["frame_pump"] == [
        {
            "cell": 0,
            "callbacks": 100,
            "queued_updates": 25,
            "coalesced_callbacks": 75,
            "ignored_callbacks": 0,
            "pending": False,
            "closed": True,
        }
    ]


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
