"""Hyperwall — macOS video surface (libmpv render API).

mpv's Swift macOS backend does NOT support --wid window embedding (mpv
maintainer in mpv-examples#29: "isn't supported by the new swift backend";
independently confirmed by IPTVnator — audio with a black video surface).
The only supported embed path on macOS is the render API: the cell's mpv
runs vo=libmpv and renders into this QOpenGLWidget's framebuffer. This is
the same architecture IINA and IPTVnator use.

Threading rules honored here (libmpv render.h + CLAUDE.md observer rules):
- The update callback fires on an mpv thread. It must not call mpv or touch
  Qt state — it only performs a bare signal emit, which Qt queues onto the
  GUI thread where update() schedules paintGL.
- Every mpv_render_* call happens on the GUI thread with this widget's GL
  context current (initializeGL / paintGL / explicit makeCurrent pairs).
- The render context is freed BEFORE the mpv core is terminated
  (render.h: freeing after core destruction is undefined behavior).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QOpenGLContext, QPainter
from PyQt6.QtOpenGLWidgets import QOpenGLWidget

from .render_telemetry import RenderTelemetry
from .frame_pump import FramePumpGate

logger = logging.getLogger("HyperWall")


class MpvGLWidget(QOpenGLWidget):
    """Per-cell video surface backed by mpv's OpenGL render context."""

    # mpv thread → GUI thread "frame available" hop. Bare emit only.
    sig_frame_ready = pyqtSignal()
    # pool thread → GUI thread "free the render ctx" hop (queued).
    _sig_free = pyqtSignal()

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._mpv: Any = None            # python-mpv MPV (vo=libmpv)
        self._ctx: Any = None            # mpv.MpvRenderContext
        self._gl_ready = False
        self._accepting_frames = True    # shutdown silences the update cb
        self._get_proc_address: Any = None  # CFUNCTYPE — must stay alive
        # Retain contexts abandoned after GL teardown until their mpv core exits.
        self._abandoned_contexts: list[Any] = []
        self._render_telemetry = RenderTelemetry()
        self._frame_pump = FramePumpGate()
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.sig_frame_ready.connect(
            self._schedule_frame_update, Qt.ConnectionType.QueuedConnection
        )
        self._sig_free.connect(self._free_ctx, Qt.ConnectionType.QueuedConnection)

    # ── lifecycle ─────────────────────────────────────────────────────

    def attach_mpv(self, m: Any) -> None:
        """Bind an MPV created with vo=libmpv.

        Creates the render context immediately when GL is up, otherwise at
        initializeGL — which always runs before the first paint, i.e. well
        before the staggered first loadfile reaches the VO (render.h: video
        init fails if no render context exists by then).
        """
        self._mpv = m
        if self._gl_ready and self._ctx is None:
            self.makeCurrent()
            try:
                self._create_render_ctx()
            finally:
                self.doneCurrent()

    def release(self) -> None:
        """Free the render context (before the mpv core is terminated).

        NEVER raises: cell.release() aborts the whole teardown chain (mpv
        terminate never runs) if this throws — then the live vo thread
        keeps firing the update callback into a dying widget → segfault
        in pyqtBoundSignal_emit (shipped once, M5 Air 2026-07-21).

        Qt qFatals (SIGABRT) on a cross-thread makeCurrent, and the wall's
        shutdown terminates cells on a ThreadPoolExecutor — so: silence
        the callback FIRST (synchronous, any thread), free synchronously
        on the widget's own (GUI) thread, queue a best-effort free for
        off-GUI callers. If the loop is already dead, process exit
        reclaims the context.
        """
        try:
            self._accepting_frames = False
            self._frame_pump.close()
            if self._ctx is not None:
                # The callback body is gated above. Do not replace update_cb
                # here. python-mpv releases the old
                # CFUNCTYPE trampoline before its libmpv setter waits for the
                # update lock; the vo thread can still be inside that old
                # callback. The guarded callback remains alive through free().
                if QThread.currentThread() is self.thread():
                    self._free_ctx()
                else:
                    self._sig_free.emit()
        except Exception as e:
            logger.debug("video frame release raised: %s", e)

    def _free_ctx(self) -> None:
        """Free the render context. GUI thread only."""
        if self._ctx is None:
            return
        self.makeCurrent()
        if QOpenGLContext.currentContext() is None:
            # The native window is already gone; freeing without a current
            # context is UB. Keep the wrapper alive while the owning mpv core
            # exits, or its ctypes callback trampoline could be released while
            # libmpv is still able to invoke it.
            logger.debug("GL gone at release — retaining render ctx.")
            self._abandoned_contexts.append(self._ctx)
            self._ctx = None
            return

        ctx, self._ctx = self._ctx, None
        try:
            ctx.free()
        except Exception as e:
            # Keep the callback trampoline alive if the binding refuses the
            # native free; the owning core may still invoke it.
            self._abandoned_contexts.append(ctx)
            logger.debug("mpv render ctx free raised: %s", e)
        finally:
            self.doneCurrent()

    # ── GL plumbing ───────────────────────────────────────────────────

    def initializeGL(self) -> None:
        self._gl_ready = True
        if self._mpv is not None and self._ctx is None:
            self._create_render_ctx()

    def _create_render_ctx(self) -> None:
        import mpv as _mpv

        def _resolve(_ctx: int, name: bytes) -> int:
            # NEVER raise in here: an exception inside a ctypes callback is
            # swallowed by the FFI, libmpv then calls the garbage pointer it
            # got back → bus error (shipped once, M5 Air 2026-07-21).
            # PyQt6 getProcAddress wants bytes/QByteArray, NOT str.
            try:
                glctx = QOpenGLContext.currentContext()
                if glctx is None:
                    return 0
                addr = glctx.getProcAddress(name)
                return int(addr) if addr else 0
            except Exception:
                return 0

        # libmpv stores the raw function pointer for the render context's
        # lifetime and may resolve lazily — keep the CFUNCTYPE alive on self.
        self._get_proc_address = _mpv.MpvGlGetProcAddressFn(_resolve)
        self._ctx = _mpv.MpvRenderContext(
            self._mpv,
            "opengl",
            opengl_init_params={"get_proc_address": self._get_proc_address},
        )
        self._accepting_frames = True
        self._ctx.update_cb = self._on_mpv_frame
        logger.debug("mpv render context created (opengl).")

    def _on_mpv_frame(self) -> None:
        """mpv vo thread → frame available. NEVER raise (ctypes callback).

        Do NOT store `self.sig_frame_ready.emit` here directly: during
        interpreter/shutdown teardown the callback can fire while the
        widget is being destroyed, and pyqtBoundSignal_emit on a dying
        object segfaults (crash 303B40DE, thread 'vo'). The flag goes
        False synchronously at release() — before anything that can fail.
        """
        try:
            if self._accepting_frames:
                self._render_telemetry.record_frame_ready()
                if self._frame_pump.request():
                    self.sig_frame_ready.emit()
        except Exception:
            pass

    def _schedule_frame_update(self) -> None:
        """GUI-thread delivery of one coalesced frame notification."""
        self.update()

    # ── painting ──────────────────────────────────────────────────────

    def paintGL(self) -> None:
        self._frame_pump.begin_paint()
        paint_started = time.perf_counter()
        render_started: float | None = None
        rendered = False
        render_ms = 0.0
        try:
            if self._ctx is None:
                # No mpv yet (staggered startup) — paint solid black so the
                # composited cell matches the Windows background.
                p = QPainter(self)
                p.fillRect(self.rect(), Qt.GlobalColor.black)
                p.end()
                return
            dpr = self.devicePixelRatioF()
            w = max(1, int(self.width() * dpr))
            h = max(1, int(self.height() * dpr))
            render_started = time.perf_counter()
            self._ctx.render(
                opengl_fbo={
                    "fbo": int(self.defaultFramebufferObject()),
                    "w": w,
                    "h": h,
                },
                flip_y=True,                 # Qt FBO origin is bottom-left
                block_for_target_time=False,  # never block the GUI thread
            )
            self._ctx.report_swap()
            rendered = True
        except Exception as e:
            logger.debug("mpv render raised: %s", e)
        finally:
            if render_started is not None:
                render_ms = (time.perf_counter() - render_started) * 1000.0
            paint_ms = (time.perf_counter() - paint_started) * 1000.0
            self._render_telemetry.record_paint(
                paint_ms=paint_ms,
                render_ms=render_ms,
                rendered=rendered,
                render_attempted=render_started is not None,
            )
            if self._frame_pump.finish_paint():
                try:
                    self.sig_frame_ready.emit()
                except Exception:
                    pass

    def telemetry_snapshot(
        self, *, reset_interval: bool = False,
    ) -> dict[str, Any]:
        """Return bounded render and frame-pump telemetry."""
        snapshot = self._render_telemetry.snapshot(reset_interval=reset_interval)
        snapshot["frame_pump"] = self._frame_pump.snapshot()
        return snapshot
