"""Emergency Escape tests: production methods, blocked fakes, no live wall.

The process-exit function is replaced in every test. Native/context owners stay
alive, while actual Python daemon workers exercise blocked cleanup and deadlines.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest import SkipTest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_class(filename, class_name, methods, namespace, *, bases=()):
    path = ROOT / "hyperwall" / filename
    tree = ast.parse(path.read_text(), filename=str(path))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    assert {n.name for n in cls.body} == set(methods)
    cls.bases = [ast.Name(id=name, ctx=ast.Load()) for name in bases]
    cls.decorator_list = []
    tree.body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls]
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), namespace)
    return namespace[class_name]


class Native:
    def __init__(self, gate):
        self.gate = gate
        self.entered = threading.Event()
        self.calls = []

    def __setitem__(self, key, value):
        self.calls.append((key, value, threading.get_ident()))
        self.entered.set()
        self.gate.wait(2)

    def command(self, *args):
        self.calls.append((*args, threading.get_ident()))

    def terminate(self):
        raise AssertionError("Emergency exit must retain the core with its GL context")


@contextmanager
def harness():
    gate, exited = threading.Event(), threading.Event()
    threads = []
    app = SimpleNamespace(setQuitOnLastWindowClosed=Mock(), quit=Mock())
    exit_times = []

    def exit_stub(status):
        assert status == 0
        exit_times.append(time.monotonic())
        exited.set()

    def worker(**kwargs):
        thread = threading.Thread(**kwargs)
        threads.append(thread)
        return thread

    def timer(*args, **kwargs):
        thread = threading.Timer(*args, **kwargs)
        threads.append(thread)
        return thread

    wall_tree = ast.parse((ROOT / "hyperwall/wall.py").read_text())
    grace = next(ast.literal_eval(n.value) for n in wall_tree.body
                 if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "EMERGENCY_EXIT_GRACE_S"
                         for t in n.targets))
    assert 0 < grace <= 0.25, "Emergency grace exceeds the 250ms target"
    namespace = {
        "_time": time,
        "threading": SimpleNamespace(Timer=timer, Thread=worker),
        "os": SimpleNamespace(_exit=exit_stub),
        "QApplication": SimpleNamespace(instance=lambda: app),
        "STATS_ENABLED": True,
        "EMERGENCY_EXIT_GRACE_S": grace,
    }
    wall_class = load_class("wall.py", "WallController", {
        "_emergency_shutdown", "_start_emergency_worker", "_emergency_session_cleanup",
    }, namespace)
    cell_class = load_class("cell.py", "VideoCell", {
        "prepare_emergency_shutdown", "stop_for_emergency_exit",
    }, {})
    gl_class = load_class("macembed.py", "MpvGLWidget", {
        "quiesce_for_exit", "_schedule_frame_update", "paintGL", "_on_mpv_frame",
    }, {})
    wall = wall_class()
    wall._emergency_exit_started = wall._shutdown_requested = wall._cleaned_up = False
    wall.windows = [SimpleNamespace(hide=Mock()), SimpleNamespace(hide=Mock())]
    wall._solo_cell = object()
    wall._session_cleanup_timer = SimpleNamespace(stop=Mock())
    wall._local_telemetry = SimpleNamespace(record_event=Mock())
    wall._api_pool = SimpleNamespace(shutdown=Mock())
    wall._api_pool_closed = False
    wall.cells = []
    for _ in range(2):
        cell = cell_class()
        cell._audio_arm_call_lock = threading.Lock()
        cell._mpv = Native(gate)
        cell._closing = False
        cell._stop_qt_timers = Mock()
        cell._emby_item_id, cell._emby_session_id = "item", "session"
        cell._prefetched = None
        frame = gl_class()
        frame._ctx = object()
        frame._get_proc_address = object()
        frame._accepting_frames = True
        frame._swap_pending_ctx = frame._ctx
        frame.update = Mock()
        cell.video_frame = frame
        wall.cells.append(cell)
    session_entered, stats_entered = threading.Event(), threading.Event()

    def blocked_sessions(*_args, **_kwargs):
        session_entered.set()
        gate.wait(2)

    def blocked_stats():
        stats_entered.set()
        gate.wait(2)

    wall.stop_emby_session = blocked_sessions
    wall._session_broker = SimpleNamespace(shutdown_records=lambda: ())
    wall._dump_stats_json = blocked_stats
    try:
        yield SimpleNamespace(wall=wall, gate=gate, exited=exited, threads=threads,
                              app=app, exit_times=exit_times, session_entered=session_entered,
                              stats_entered=stats_entered)
    finally:
        gate.set()
        for thread in threads:
            thread.join(timeout=1)


def test_escape_returns_and_hides_without_waiting_for_blocked_cleanup():
    with harness() as h:
        started = time.monotonic()
        h.wall._emergency_shutdown()
        elapsed = time.monotonic() - started
        assert elapsed < 0.2, f"GUI handler blocked for {elapsed:.3f}s"
        for window in h.wall.windows:
            window.hide.assert_called_once()
        for cell in h.wall.cells:
            assert cell._closing and not cell.video_frame._accepting_frames
            assert cell._mpv.entered.wait(0.2)
            assert all(call[-1] != threading.get_ident() for call in cell._mpv.calls)
        assert h.session_entered.wait(0.2) and h.stats_entered.wait(0.2)
        assert h.exited.wait(0.5), "Blocked cleanup prevented the exit deadline"
        assert h.exit_times[0] - started < 0.6
        assert not h.gate.is_set(), "Test released the blockers before exit"
        assert all(thread.daemon for thread in h.threads)
        h.app.quit.assert_not_called()
        h.wall._local_telemetry.record_event.assert_called_once()
        event = h.wall._local_telemetry.record_event.call_args
        assert event.args == ("escape",) and event.kwargs["windows_hidden_ms"] < 200
        print(f"    observed handler={elapsed * 1000:.2f}ms, mocked exit={(h.exit_times[0]-started)*1000:.2f}ms")


def test_escape_stops_conversion_queue_after_hiding_without_waiting_for_worker():
    from hyperwall.transcode_queue import FailureTranscodeQueue

    with harness() as h:
        # Use the production queue stop method with a real daemon blocked in
        # simulated server I/O. No queue constructor, disk access or HTTP runs.
        queue_worker = object.__new__(FailureTranscodeQueue)
        queue_worker._stop = threading.Event()
        entered = threading.Event()

        def blocked_conversion():
            entered.set()
            h.gate.wait(2)

        queue_worker.thread = threading.Thread(target=blocked_conversion, daemon=True)
        h.threads.append(queue_worker.thread)
        queue_worker.thread.start()
        assert entered.wait(.2)
        real_stop = queue_worker.stop

        def stop_after_hide():
            assert all(window.hide.call_count == 1 for window in h.wall.windows)
            real_stop()

        queue_worker.stop = Mock(side_effect=stop_after_hide)
        h.wall._failure_transcodes = queue_worker
        with patch.object(queue_worker.thread, "join", side_effect=AssertionError("Escape joined conversion worker")) as join:
            started = time.monotonic()
            h.wall._emergency_shutdown()
            assert time.monotonic() - started < .2
            queue_worker.stop.assert_called_once()
            assert queue_worker._stop.is_set()
            assert queue_worker.thread.is_alive() and not h.gate.is_set()
            assert h.exited.wait(.5)
            join.assert_not_called()


def test_escape_is_idempotent_and_keeps_render_callback_owners_alive():
    with harness() as h:
        owners = [(c._mpv, c.video_frame._ctx, c.video_frame._get_proc_address) for c in h.wall.cells]
        h.wall._emergency_shutdown()
        h.wall._emergency_shutdown()
        assert h.exited.wait(0.5)
        assert len(h.exit_times) == 1
        for cell, (mpv, ctx, callback) in zip(h.wall.cells, owners):
            assert cell._mpv is mpv
            assert cell.video_frame._ctx is ctx
            assert cell.video_frame._get_proc_address is callback
            # Already queued Qt paints/frame callbacks must not re-enter mpv.
            cell.video_frame.paintGL()
            cell.video_frame._on_mpv_frame()
            cell.video_frame._schedule_frame_update()
            cell.video_frame.update.assert_not_called()


def test_busy_native_owner_never_delays_escape_or_enters_native_stop():
    with harness() as h:
        for cell in h.wall.cells:
            cell._audio_arm_call_lock.acquire()
        try:
            h.wall._emergency_shutdown()
            assert h.exited.wait(0.5)
            assert all(not c._mpv.calls for c in h.wall.cells)
        finally:
            for cell in h.wall.cells:
                cell._audio_arm_call_lock.release()


def test_watchdog_is_armed_before_a_blocked_window_hide():
    with harness() as h:
        def hide():
            assert h.exited.wait(0.5), "Exit watchdog was not armed before window hide"
        h.wall.windows[0].hide = hide
        h.wall._emergency_shutdown()
        assert h.exited.is_set()


def test_one_real_qt_escape_exits_even_when_solo_is_active():
    try:
        from PyQt6.QtCore import QEvent, QObject, Qt
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtWidgets import QApplication, QWidget
    except ImportError:
        raise SkipTest("PyQt6 unavailable")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    namespace = {"QEvent": QEvent, "QObject": QObject, "Qt": Qt}
    filter_class = load_class("wall.py", "EmergencyKeyFilter", {"__init__", "eventFilter"},
                              namespace, bases=("QObject",))
    shutdown, exit_solo = Mock(), Mock()
    event_filter = filter_class(shutdown, lambda: True, exit_solo)
    child = QWidget()  # Never shown; no wall, GL context or media is created.
    app.installEventFilter(event_filter)
    try:
        event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(child, event)
        shutdown.assert_called_once()
        exit_solo.assert_not_called()
    finally:
        app.removeEventFilter(event_filter)


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = skipped = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except SkipTest as exc:
            skipped += 1
            print(f"  SKIP  {test.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc!r}")
    print(f"  {len(tests)-failed-skipped} passed, {failed} failed, {skipped} skipped")
    return failed


if __name__ == "__main__":
    raise SystemExit(run_all())
