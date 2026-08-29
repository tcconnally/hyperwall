"""Contract tests for per-resource decoder observability."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(_ROOT))


def test_cell_telemetry_exports_decoder_state():
    source = (_ROOT / "hyperwall" / "cell.py").read_text(encoding="utf-8")
    assert '"decoder"' in source
    assert "_decoder_fault_count" in source
    assert "_force_software_decode" in source
    assert "_resource_quarantined" in source


def test_stats_dump_exports_decoder_state_separately():
    source = (_ROOT / "hyperwall" / "wall.py").read_text(encoding="utf-8")
    assert '"decoder": telemetry.get("decoder", {})' in source


def test_stats_summary_projects_decoder_state():
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
                            "decoder": {
                                "requested": "videotoolbox-copy",
                                "active": "no",
                                "fault_count": 2,
                                "software_fallback": True,
                                "resource_quarantined": False,
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        summary = _stats_summary(path)

    assert summary["decoder"] == [
        {
            "cell": 0,
            "requested": "videotoolbox-copy",
            "active": "no",
            "fault_count": 2,
            "software_fallback": True,
            "resource_quarantined": False,
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
