"""Source-level contract for integrating the frame gate with macOS libmpv."""
from __future__ import annotations

import os
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def _source() -> str:
    return (_ROOT / "hyperwall" / "macembed.py").read_text(encoding="utf-8")


def test_mpv_callback_admits_only_coalesced_frame_notifications():
    source = _source()
    assert "from .frame_pump import FramePumpGate" in source
    assert "self._frame_pump = FramePumpGate()" in source
    assert "if self._frame_pump.request():" in source
    assert "self._frame_pump.request()" in source


def test_paint_lifecycle_requeues_a_frame_arriving_during_render():
    source = _source()
    assert "self._frame_pump.begin_paint()" in source
    assert "self._frame_pump.finish_paint()" in source
    assert "self.sig_frame_ready.emit()" in source


def test_release_closes_frame_gate_before_context_teardown():
    source = _source()
    release_start = source.index("    def release(self)")
    free_start = source.index("    def _free_ctx", release_start)
    release = source[release_start:free_start]
    assert "self._frame_pump.close()" in release
    assert release.index("self._frame_pump.close()") < release.index("self._free_ctx()")


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
