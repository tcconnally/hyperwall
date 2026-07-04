"""
Unit tests for hyperwall.theme — pure palette + QSS string helpers.

No PyQt import needed (theme keeps Qt out of module load). Run:
  python tests/test_theme.py
"""

from __future__ import annotations

import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from hyperwall import theme  # noqa: E402


def test_rgba_basic():
    assert theme.rgba("#3b8edb", 0.5) == "rgba(59, 142, 219, 0.500)"


def test_rgba_handles_no_hash():
    assert theme.rgba("000000", 1.0) == "rgba(0, 0, 0, 1.000)"


def test_palette_constants_are_hex():
    for name in ("ACCENT", "SURFACE_0", "TEXT", "BORDER", "DANGER"):
        val = getattr(theme, name)
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", val), f"{name}={val!r} not #rrggbb"


def test_accent_is_brand_blue():
    # The web remote hard-codes #3b8edb; keep the desktop accent in lockstep.
    assert theme.ACCENT.lower() == "#3b8edb"


def test_dialog_qss_is_populated_and_themed():
    qss = theme.dialog_qss()
    assert "QDialog" in qss and "QPushButton" in qss
    # references the palette, not stray literals
    assert theme.ACCENT in qss
    assert theme.SURFACE_0 in qss


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
