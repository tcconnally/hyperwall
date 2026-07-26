"""Regression contract for the macOS soak findings (2026-07-26).

The M5 soak measured a 210 ms mute/unmute slot and repeated 170–246 ms
next/prefetch slots. These tests pin the two low-risk corrections: relock from
our event-thread-maintained position cache (not synchronous libmpv property
IPC), and defer playlist prefetch until the current transition returns to Qt.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(relative: str) -> str:
    with open(os.path.join(_ROOT, relative), encoding="utf-8") as f:
        return f.read()


def test_audio_relock_uses_observed_position_cache():
    source = _source("hyperwall/cell.py")
    start = source.index("    def _enable_audio_track")
    end = source.index("\n    def _sync_mute_ui", start)
    body = source[start:end]
    assert "self._play_pos" in body
    assert "self._mpv.time_pos" not in body


def test_prefetch_is_deferred_after_transition():
    source = _source("hyperwall/wall.py")
    start = source.index("    def _arm_prefetch")
    end = source.index("\n    def run_on_main", start)
    body = source[start:end]
    assert "QTimer.singleShot(0," in body
    assert "def _queue" in body


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
