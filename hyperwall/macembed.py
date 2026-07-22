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
from typing import Any

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QOpenGLContext, QPainter
from PyQt6.QtOpenGLWidgets import QOpenGLWidget

logger = logging.getLogger("HyperWall")


class MpvGLWidget(QOpenGLWidget):
    """Per-cell video surface backed by mpv's OpenGL render context."""

    # mpv thread → GUI thread "frame available" hop. Bare emit only.
    sig_frame_ready = pyqtSignal()

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._mpv: Any = None            # python-mpv MPV (vo=libmpv)
        self._ctx: Any = None            # mpv.MpvRenderContext
        self._gl_ready = False
        self._get_proc_address: Any = None  # CFUNCTYPE — must stay alive
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.sig_frame_ready.connect(
            self.update, Qt.ConnectionType.QueuedConnection
        )

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

        Qt qFatals (SIGABRT) on a cross-thread makeCurrent, and the wall's
        shutdown terminates cells on a ThreadPoolExecutor — the first macOS
        exit crashed exactly there. Free synchronously on the widget's own
        (GUI) thread; off-GUI callers get a queued best-effort free — if
        the loop is already dead, process exit reclaims the context.
        """
        if self._ctx is None:
            return
        if QThread.currentThread() is self.thread():
            self._free_ctx()
        else:
            QTimer.singleShot(0, self, self._free_ctx)

    def _free_ctx(self) -> None:
        """Free the render context. GUI thread only."""
        if self._ctx is None:
            return
        self.makeCurrent()
        if QOpenGLContext.currentContext() is None:
            # Native window already torn down (shutdown) — freeing with no
            # current context is UB. Leak it; the process is exiting.
            logger.debug("GL gone at release — abandoning render ctx.")
            self._ctx = None
            return
        try:
            ctx, self._ctx = self._ctx, None
            ctx.free()
        except Exception as e:
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
        # Bare emit — this fires on an mpv thread (observer rules).
        self._ctx.update_cb = self.sig_frame_ready.emit
        logger.debug("mpv render context created (opengl).")

    # ── painting ──────────────────────────────────────────────────────

    def paintGL(self) -> None:
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
        try:
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
        except Exception as e:
            logger.debug("mpv render raised: %s", e)
