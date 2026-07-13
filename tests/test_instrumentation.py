"""Construction/wiring tests for the opt-in instrumentation.

Regression for the 10.6.0 soak-launch crash: LoopLagSampler was handed the
WallController as a QObject parent, but WallController is a plain object —
the app died right after wall init whenever HYPERWALL_PERFTRACE=1. These
tests construct both instruments exactly the way app.py wires them, against
a non-QObject wall stand-in, and drive their timer slots directly.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HYPERWALL_SOAK_MINUTES"] = "1"

from PyQt6.QtCore import QCoreApplication

_app = QCoreApplication.instance() or QCoreApplication([])


class _FakeCell:
    current_item = {"Name": "x", "Id": "1"}


class _PlainWall:
    """Deliberately NOT a QObject — mirrors WallController."""

    def __init__(self):
        self.cells = [_FakeCell(), _FakeCell()]
        self.advances = []
        self.shutdowns = 0

    def next_video(self, cell, is_retry=False):
        self.advances.append(cell)

    def _shutdown(self):
        self.shutdowns += 1


def test_loop_lag_sampler_constructs_and_ticks():
    from hyperwall.perftrace import LoopLagSampler
    s = LoopLagSampler()  # app.py passes no parent — wall is not a QObject
    s.start()
    s._tick()
    s._tick()
    s._log_summary()  # must not raise with samples present
    assert True


def test_soak_controller_constructs_against_plain_wall():
    from hyperwall.soak import SoakController
    wall = _PlainWall()
    c = SoakController(wall)  # regression: must not use wall as QObject parent
    c._sample()               # resource snapshot logs without raising
    c._churn()                # advances a random cell via wall.next_video
    assert len(wall.advances) == 1
    c._finish()               # summary + graceful shutdown hook
    assert wall.shutdowns == 1


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
    raise SystemExit(1 if run_all() else 0)
