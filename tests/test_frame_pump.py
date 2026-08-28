"""Contract tests for the thread-safe macOS frame-pump gate."""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperwall.frame_pump import FramePumpGate


def test_first_callback_queues_one_notification_and_burst_is_coalesced():
    gate = FramePumpGate()

    assert gate.request() is True
    assert gate.request() is False
    assert gate.request() is False

    snapshot = gate.snapshot()
    assert snapshot["callbacks"] == 3
    assert snapshot["queued_updates"] == 1
    assert snapshot["coalesced_callbacks"] == 2
    assert snapshot["pending"] is True


def test_paint_completion_requeues_when_a_new_frame_arrives_during_paint():
    gate = FramePumpGate()

    assert gate.request() is True
    gate.begin_paint()
    assert gate.request() is False

    assert gate.finish_paint() is True
    snapshot = gate.snapshot()
    assert snapshot["queued_updates"] == 2
    assert snapshot["pending"] is True


def test_paint_completion_clears_pending_when_no_new_frame_arrived():
    gate = FramePumpGate()

    assert gate.request() is True
    gate.begin_paint()
    assert gate.finish_paint() is False

    assert gate.snapshot()["pending"] is False
    assert gate.request() is True


def test_close_rejects_callbacks_and_clears_pending_state():
    gate = FramePumpGate()
    assert gate.request() is True

    gate.close()

    assert gate.request() is False
    snapshot = gate.snapshot()
    assert snapshot["closed"] is True
    assert snapshot["pending"] is False
    assert snapshot["ignored_callbacks"] == 1


def test_callback_and_paint_completion_are_safe_when_racing():
    gate = FramePumpGate()
    assert gate.request() is True
    gate.begin_paint()

    start = threading.Barrier(2)
    results: list[bool] = []

    def callback() -> None:
        start.wait()
        results.append(gate.request())

    worker = threading.Thread(target=callback)
    worker.start()
    start.wait()
    requeued = gate.finish_paint()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert (
        (requeued is True and results == [False])
        or (requeued is False and results == [True])
    )
    snapshot = gate.snapshot()
    assert snapshot["queued_updates"] == 2
    assert snapshot["pending"] is True


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
