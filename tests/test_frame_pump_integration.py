"""Frame-gate wiring and offscreen Qt presentation lifecycle tests."""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from importlib.util import find_spec
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest import SkipTest
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(_ROOT))
_APP = None


def _source() -> str:
    return (_ROOT / "hyperwall" / "macembed.py").read_text(encoding="utf-8")


def _requires_qt(test):
    @wraps(test)
    def run():
        if find_spec("PyQt6") is None:
            raise SkipTest("PyQt6 is not installed in the pure-logic CI lane")
        return test()
    return run


def test_mpv_callback_admits_only_coalesced_frame_notifications():
    source = _source()
    assert "from .frame_pump import FramePumpGate" in source
    assert "self._frame_pump = FramePumpGate()" in source
    assert "if frame_pump.request():" in source
    assert "frame_pump is not self._frame_pump" in source


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


@contextmanager
def _surface():
    """Exercise real widget slots/signals without a window, GPU, or mpv core."""
    global _APP
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from hyperwall import macembed

    _APP = QApplication.instance() or QApplication([])
    current = {"context": None}
    reports = []
    renders = []
    frees = []

    class GlContext:
        def surface(self):
            return self

        def makeCurrent(self, surface):
            assert surface is self
            current["context"] = self
            return True

    owner = GlContext()
    compositor = GlContext()
    current["context"] = compositor

    class RenderContext:
        fail_report = False
        fail_free = False

        def __init__(self, *_args, **_kwargs):
            pass

        def render(self, **kwargs):
            assert current["context"] is owner
            assert kwargs["block_for_target_time"] is False
            renders.append(self)

        def report_swap(self):
            assert current["context"] is owner
            reports.append(self)
            if self.fail_report:
                raise RuntimeError("simulated report failure")

        def free(self):
            frees.append(self)
            if self.fail_free:
                raise RuntimeError("simulated native free failure")

    with patch.object(
        macembed, "QOpenGLContext",
        SimpleNamespace(currentContext=lambda: current["context"]),
    ):
        widget = macembed.MpvGLWidget()
        ctx = RenderContext()
        widget._ctx = ctx
        widget.context = lambda: owner
        widget.makeCurrent = lambda: current.update(context=owner)
        widget.doneCurrent = lambda: current.update(context=None)

        def paint():
            current["context"] = owner
            widget.paintGL()
            current["context"] = compositor

        try:
            yield SimpleNamespace(
                widget=widget, ctx=ctx, paint=paint, current=current,
                owner=owner, compositor=compositor, reports=reports,
                renders=renders, frees=frees, new_context=RenderContext,
            )
        finally:
            widget._accepting_frames = False
            widget._ctx = None
            widget.deleteLater()


@_requires_qt
def test_swap_is_reported_after_qt_presentation_and_restores_compositor():
    with _surface() as s:
        s.widget.frameSwapped.emit()
        assert s.reports == []
        s.paint()
        assert s.renders == [s.ctx]
        assert s.reports == []  # Rendering the FBO is not presentation.
        s.widget.frameSwapped.emit()
        assert s.reports == [s.ctx]
        assert s.current["context"] is s.compositor
        s.widget.frameSwapped.emit()
        assert s.reports == [s.ctx]  # No second presentation of a new render.
        # Several redraws can be coalesced into one window presentation.
        # Report that single swap once, then continue reporting later swaps.
        s.paint()
        s.paint()
        s.paint()
        assert len(s.renders) == 4 and len(s.reports) == 1
        s.widget.frameSwapped.emit()
        assert len(s.reports) == 2
        s.widget.frameSwapped.emit()
        assert len(s.reports) == 2
        s.paint()
        s.widget.frameSwapped.emit()
        assert len(s.reports) == 3


@_requires_qt
def test_late_swap_does_not_target_replacement_or_released_context():
    with _surface() as s:
        s.paint()
        replacement = s.new_context()
        s.widget._ctx = replacement
        s.widget.frameSwapped.emit()
        assert s.reports == []
        s.paint()
        s.widget.frameSwapped.emit()
        assert s.reports == [replacement]
        s.paint()
        s.widget.release()
        assert s.frees == [replacement]
        assert s.widget._swap_pending_ctx is None
        s.widget.frameSwapped.emit()
        assert s.reports == [replacement]


@_requires_qt
def test_swap_skips_missing_or_unavailable_gl_context():
    with _surface() as s:
        s.paint()
        s.widget.context = lambda: None
        s.widget.frameSwapped.emit()
        assert s.reports == []
        s.widget.context = lambda: s.owner
        s.paint()
        s.widget.makeCurrent = lambda: None
        s.widget.frameSwapped.emit()
        assert s.reports == []
        assert s.current["context"] is s.compositor


@_requires_qt
def test_swap_restores_context_when_native_report_raises():
    with _surface() as s:
        s.paint()
        s.ctx.fail_report = True
        s.widget.frameSwapped.emit()
        assert s.reports == [s.ctx]
        assert s.current["context"] is s.compositor


@_requires_qt
def test_swap_releases_gl_context_when_none_was_current():
    with _surface() as s:
        s.paint()
        s.current["context"] = None
        s.widget.frameSwapped.emit()
        assert s.reports == [s.ctx]
        assert s.current["context"] is None


@_requires_qt
def test_swap_rejects_off_thread_delivery_and_quiesced_widget():
    from hyperwall import macembed

    with _surface() as s:
        s.paint()
        with patch.object(
            macembed, "QThread", SimpleNamespace(currentThread=lambda: object()),
        ):
            s.widget._on_frame_swapped()
        assert s.reports == []
        assert s.current["context"] is s.compositor
        s.widget._accepting_frames = False
        s.widget.frameSwapped.emit()
        assert s.reports == []


@contextmanager
def _recreated_surface():
    with _surface() as s:
        # Real attach/create/release logic; native wrappers and GL are fakes.
        fake_mpv = SimpleNamespace(MpvRenderContext=s.new_context,
                                   MpvGlGetProcAddressFn=lambda callback: callback)
        with patch.dict(sys.modules, {"mpv": fake_mpv}):
            s.widget._ctx = None
            s.widget._gl_ready = True
            s.widget.attach_mpv(object())
            yield s


@_requires_qt
def test_recreated_context_uses_fresh_gate_and_rejects_late_old_callback():
    with _recreated_surface() as s:
        first_ctx, first_gate = s.widget._ctx, s.widget._frame_pump
        first_callback = first_ctx.update_cb
        notifications = []
        s.widget.sig_frame_ready.connect(lambda: notifications.append(True))
        first_callback()
        first_callback()
        assert len(notifications) == 1
        s.paint()
        s.widget.release()
        assert first_gate.snapshot()["closed"] is True
        assert first_ctx.update_cb is first_callback  # Never clear a live FFI trampoline.
        s.widget.attach_mpv(object())
        second_ctx, second_gate = s.widget._ctx, s.widget._frame_pump
        assert second_ctx is not first_ctx and second_gate is not first_gate
        assert second_gate.snapshot()["closed"] is False
        first_callback()
        assert second_gate.snapshot()["callbacks"] == 0
        assert len(notifications) == 1
        second_ctx.update_cb()
        second_ctx.update_cb()
        assert len(notifications) == 2
        assert second_gate.snapshot()["coalesced_callbacks"] == 1
        s.paint()
        assert s.renders == [first_ctx, second_ctx]
        second_ctx.update_cb()
        assert len(notifications) == 3  # Subsequent frames remain live.


@_requires_qt
def test_callback_paused_during_recreation_never_marks_fresh_gate_pending():
    with _recreated_surface() as s:
        old_ctx = s.widget._ctx
        entered, resume = threading.Event(), threading.Event()

        def paused_record():
            entered.set()
            assert resume.wait(2)

        with patch.object(s.widget._render_telemetry, "record_frame_ready", paused_record):
            callback = threading.Thread(target=old_ctx.update_cb, daemon=True)
            callback.start()
            try:
                assert entered.wait(1)
                s.widget.release()
                s.widget.attach_mpv(object())
                fresh = s.widget._frame_pump
            finally:
                resume.set()
                callback.join(timeout=1)
            assert not callback.is_alive()
            assert fresh.snapshot()["callbacks"] == 0
            assert fresh.snapshot()["pending"] is False
        s.widget._ctx.update_cb()
        assert fresh.snapshot()["pending"] is True


@_requires_qt
def test_abandoned_context_keeps_its_resolver_and_callback_after_recreation():
    with _recreated_surface() as s:
        old_ctx = s.widget._ctx
        resolver, callback = old_ctx._hyperwall_get_proc_address, old_ctx.update_cb
        old_ctx.fail_free = True
        s.widget.release()
        assert old_ctx in s.widget._abandoned_contexts
        s.widget.attach_mpv(object())
        assert old_ctx._hyperwall_get_proc_address is resolver
        assert old_ctx.update_cb is callback
        assert s.widget._get_proc_address is not resolver
        callback()
        assert s.widget._frame_pump.snapshot()["callbacks"] == 0


def run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    passed = failed = skipped = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS  {test.__name__}")
        except SkipTest as exc:
            skipped += 1
            print(f"  SKIP  {test.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"  {passed} passed, {failed} failed, {skipped} skipped")
    return failed


if __name__ == "__main__":
    raise SystemExit(run_all())
