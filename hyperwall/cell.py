"""
Hyperwall — VideoCell widget.

Embeds a libmpv player in a QFrame with overlaid controls.
One VideoCell = one video in the wall grid.

Lifecycle: create() → play() → destroy()
  - create(): allocates native window, creates mpv instance
  - play(): loads a URL into the existing mpv (gapless reuse)
  - destroy(): terminates mpv, cleans up

Key fixes from v8:
  - Single create path (_ensure_mpv) with visibility + realized guard
  - HWND sign-extension mask (& 0xFFFFFFFF)
  - C stdio redirect during mpv creation (suppress FFmpeg noise)
  - Bounded mpv terminate via ThreadPoolExecutor (1.5s timeout)
  - Generation counter to ignore stale observer callbacks
"""

from __future__ import annotations

import logging
import os
import random
import sys
import threading
import time as _time
from collections import deque
from typing import Any

from PyQt6.QtCore import (
    Qt,
    QEasingCurve,
    QPropertyAnimation,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from .constants import (
    AUTOHIDE_MS,
    CONTROLS_HEIGHT,
    CONTROLS_OPACITY,
    CRASH_LOOP_COOLDOWN_S,
    CRASH_LOOP_THRESHOLD,
    CRASH_LOOP_WINDOW_S,
    MAX_RETRIES,
    MOUSE_IDLE_MS,
    MPV_LOG_NOISE,
    MPV_OPTS,
    OUTAGE_BACKOFF_S,
    OVERLAY_SHOW_MS,
    STALL_TIMEOUT_S,
    STATS_COUNTER_PROPS,
    STATS_ENABLED,
    STATS_INFO_PROPS,
    WATCHDOG_INTERVAL_MS,
    _s,
    apply_env_overrides,
    native_wid,
)
from .reliability import (
    apply_jitter,
    end_file_reason,
    escalation_plan,
    is_stalled,
    should_park,
)
from . import theme
from .perftrace import traced
from .urls import tag_names

logger = logging.getLogger("HyperWall")


# Glassy translucent control bar, on-brand accent, rounded top. Buttons are
# borderless and quiet at rest, lifting to the accent on hover/active so the
# bar reads as one slab instead of a row of grey chips.
CTRL_STYLE = f"""
    QFrame#controls {{
        background: {theme.rgba(theme.SURFACE_0, 0.90)};
        border: 1px solid {theme.rgba(theme.ACCENT_BRIGHT, 0.18)};
        border-radius: {_s(20)}px;
    }}
    QLabel {{ color: {theme.TEXT_DIM}; font-family: {theme.FONT}; font-size: {_s(10)}px; background: transparent; }}
    /* Buttons are bare glyphs on the pill surface — no chip fill at rest, so
       the bigger icons carry the identity; hover lifts to a round accent. */
    QPushButton {{
        background: transparent; border: none;
        border-radius: {_s(15)}px; color: {theme.TEXT}; font-size: {_s(15)}px; padding: 0;
        font-family: 'Segoe UI Symbol', 'Segoe UI Emoji', {theme.FONT};
        min-width: {_s(30)}px; min-height: {_s(30)}px; max-width: {_s(30)}px; max-height: {_s(30)}px;
    }}
    QPushButton:hover   {{ background: {theme.ACCENT}; color: white; }}
    QPushButton:pressed {{ background: {theme.ACCENT_DEEP}; }}
    QPushButton:checked {{ background: {theme.rgba('#ffffff', 0.10)}; color: {theme.ACCENT_BRIGHT}; }}
    /* Favorite / trash: active state tints the monochrome glyph itself
       (gold / red) via QSS — reliable regardless of whether the platform
       font honours a VS16 colour-emoji selector. */
    QPushButton#favBtn:checked {{ background: {theme.rgba('#ffffff', 0.10)}; color: {theme.FAVORITE}; }}
    QPushButton#tagBtn:checked {{ background: {theme.rgba('#ffffff', 0.10)}; color: {theme.DANGER}; }}
    /* Audible (unmuted) cell: bright speaker so live-audio cells are
       identifiable at a glance across the wall. */
    QPushButton#muteBtn[audible="true"] {{ color: {theme.ACCENT_BRIGHT}; }}
    QFrame#ctrlSep {{ background: {theme.rgba('#ffffff', 0.14)}; }}
    QSlider::groove:horizontal {{ background: {theme.rgba('#ffffff', 0.18)}; height: {_s(4)}px; border-radius: {_s(2)}px; }}
    QSlider::sub-page:horizontal {{ background: {theme.ACCENT}; border-radius: {_s(2)}px; }}
    QSlider::handle:horizontal {{
        background: #ffffff; width: {_s(12)}px; height: {_s(12)}px; margin: {_s(-4)}px 0; border-radius: {_s(6)}px;
    }}
    QSlider::handle:horizontal:hover {{ background: {theme.ACCENT_BRIGHT}; }}
"""

# Title card = translucent pill with a left accent spine. Loading card reuses
# the pill but in accent, tracked with a gentle opacity pulse (see cell init).
_TITLE_STYLE = (
    f"color: {theme.TEXT}; background: {theme.rgba(theme.SURFACE_0, 0.82)};"
    f" border-left: {_s(3)}px solid {theme.ACCENT};"
    f" font-family: {theme.FONT}; font-size: {_s(13)}px; font-weight: 700;"
    f" padding: {_s(5)}px {_s(14)}px; border-radius: {_s(4)}px;"
)
_LOADING_STYLE = (
    f"color: {theme.ACCENT_BRIGHT}; background: {theme.rgba(theme.SURFACE_0, 0.82)};"
    f" font-family: {theme.FONT}; font-size: {_s(12)}px; font-weight: 800;"
    f" letter-spacing: {_s(3)}px; padding: {_s(5)}px {_s(16)}px; border-radius: {_s(4)}px;"
)

# Icon glyphs. A trailing VS15 (U+FE0E) forces the monochrome text glyph so the
# control-bar QSS `color` can tint every icon uniformly. The bar reads
# monochrome; an *active* favorite (gold) or trash flag (red) is tinted by the
# `#favBtn:checked` / `#tagBtn:checked` rules above — no VS16 colour-emoji glyph
# is used, so the active state can't silently regress on fonts that ignore VS16.
_MONO = "︎"   # VS15 - force monochrome text glyph
_G_PREV = "⏮" + _MONO
_G_PAUSE = "⏸" + _MONO   # shown while playing (click → pause)
_G_PLAY = "▶" + _MONO    # shown while paused  (click → play)
_G_NEXT = "⏭" + _MONO
_G_LOOP = "🔁" + _MONO
_G_MUTE = "🔇" + _MONO
_G_UNMUTE = "🔊" + _MONO
_G_TRASH = "🗑" + _MONO
_G_FAV = "⭐" + _MONO


class ClickSlider(QSlider):
    """Slider that treats any left press as an absolute grab.

    The previous jump-then-delegate version moved the handle under the
    cursor and relied on QSlider's own hit-test to begin a drag. With the
    9px styled handle, the raw x→value mapping lands the handle a few px
    off near the groove ends, the hit-test misses, and Qt falls back to a
    page-step: no sliderPressed/sliderReleased, so the seek wiring never
    fires and the UI timer snaps the thumb back — click-seek only worked
    mid-bar. Owning press/move/release makes click and drag one code path;
    setSliderDown() emits the same pressed/released signals the seek
    handlers are wired to.
    """

    def _value_at(self, x: float) -> int:
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), int(x), max(1, self.width()),
        )

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderPosition(self._value_at(event.position().x()))
            self.setSliderDown(True)   # emits sliderPressed
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self.isSliderDown():
            self.setSliderPosition(self._value_at(event.position().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderDown(False)  # emits sliderReleased
            event.accept()
            return
        super().mouseReleaseEvent(event)


class VideoCell(QWidget):
    """A single video cell in the wall grid."""

    request_next = pyqtSignal(object, bool)
    request_prev = pyqtSignal(object)
    _sig_eof = pyqtSignal(int, str)
    _sig_track_done = pyqtSignal(int)
    _sig_buffering = pyqtSignal(int, bool)

    def __init__(self, controller: Any):
        super().__init__()
        self.controller = controller
        self.current_item: dict[str, Any] | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=50)
        self.looping = False
        self.muted = True
        self._last_vol = 70  # per-cell; restored when unmuting from silence
        # Controls start hidden (the frame is hide()n after build); this flag
        # must agree or the first hover is a no-op until the autohide timer
        # happens to correct it.
        self.controls_visible = False

        # Internal state
        self._mpv: Any = None          # mpv.MPV instance
        self._mpv_gen = 0              # generation counter
        self._duration_s = 0.0
        self._play_pos = 0.0
        self._dragging = False
        self._paused_before_seek = False
        # Freeze visibility: paused-for-cache episodes (network starvation).
        # These freezes are invisible to the frame-drop counters AND shorter
        # than the 20s stall watchdog — the gap the owner kept seeing.
        self._freeze_t0 = 0.0
        self._freeze_count = 0
        self._freeze_total_s = 0.0
        self._freeze_postseek_count = 0
        self._last_seek_ts = 0.0
        self._buffering_card = False
        self._retry_count = 0
        self._force_transcode = False
        # True while this cell's CURRENT stream is a server transcode (HLS).
        # Read by the controller's transcode-concurrency gate. Kept drift-free
        # by re-deriving it from the URL at every stream commit (play +
        # advance_to_prefetched); a transcode URL carries an .m3u8 playlist.
        self._is_transcoding = False
        self._played_anything = False
        self._paused = False  # main-thread cache; safe to read cross-thread
        self._last_next_request_ts = 0.0
        self._pending_next = False  # a throttled advance waiting to re-fire
        self._mouse_in_cell = False
        self._emby_session_id: str | None = None
        self._emby_item_id: str | None = None
        # True between a reuse-loadfile and the old track's stale end-file
        # (reason "stop"). NEVER set for a fresh mpv — no stale event will
        # arrive to clear it, and a latched _switching once silently disabled
        # the eof handling of every cell's first track (2026-07-11 lockup).
        self._switching = False
        self._track_done = False  # this track already triggered its advance
        self._audio_started = False  # True once this track's audio is armed
        # (item, url, emby_session_id) queued on the live mpv playlist so
        # prefetch-playlist warms its demuxer before the current track ends.
        self._prefetched: tuple[dict[str, Any], str, str] | None = None

        # Reliability / self-healing (Epic 2)
        self._last_progress_ts = 0.0   # monotonic ts of last time-pos advance
        self._last_seen_pos = -1.0     # last observed time-pos value
        self._failure_ts: deque[float] = deque(maxlen=64)  # recent failure times
        self._parked = False           # crash-loop parked → stop retrying
        # Budgeted mpv opts, set by the controller once the grid size is known
        # (memory-aware demuxer cache). None → fall back to unbudgeted defaults.
        self._mpv_opts: dict[str, Any] | None = None

        # Stats
        self._stats_current: dict[str, float] = {}
        self._stats_total: dict[str, float] = {}
        self._stats_info: dict[str, object] = {}

        self.setStyleSheet("background: black;")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # Main layout: video fills the cell, controls overlay on top
        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        # Video surface. macOS: --wid embedding is unsupported by mpv's
        # Swift backend, so cells render through the libmpv render API into
        # a QOpenGLWidget (macembed.py). Windows: native HWND embed below.
        if sys.platform == "darwin":
            from .macembed import MpvGLWidget
            self.video_frame = MpvGLWidget(self)
        else:
            self.video_frame = QFrame(self)
            self.video_frame.setStyleSheet("background: black;")
            self.video_frame.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.video_frame.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
            self.video_frame.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
            self.video_frame.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        vbox.addWidget(self.video_frame, 1)

        self._build_controls()
        self.controls_frame.setParent(self)
        self.controls_frame.hide()
        self._reposition_controls()

        # Autohide timer (armed on hover / global-show; controls start hidden
        # so there is nothing to auto-hide at startup)
        self._autohide_timer = QTimer(self)
        self._autohide_timer.setSingleShot(True)
        self._autohide_timer.timeout.connect(self._autohide_controls)

        # UI refresh timer (only runs while controls visible)
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(250)
        self._ui_timer.timeout.connect(self._refresh_progress_ui)

        # Title overlay
        self._title_overlay = QLabel("", self)
        self._title_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_overlay.setWordWrap(False)
        self._title_overlay.setStyleSheet(_TITLE_STYLE)
        self._title_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._title_overlay.hide()

        self._overlay_effect = QGraphicsOpacityEffect(self._title_overlay)
        self._title_overlay.setGraphicsEffect(self._overlay_effect)
        self._overlay_anim = QPropertyAnimation(self._overlay_effect, b"opacity", self)
        self._overlay_anim.setDuration(600)
        self._overlay_anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._overlay_anim.finished.connect(self._on_overlay_fade_done)
        self._overlay_show_timer = QTimer(self)
        self._overlay_show_timer.setSingleShot(True)
        self._overlay_show_timer.timeout.connect(self._fade_overlay_out)

        # Looping opacity pulse for the "LOADING" card. Runs only between the
        # cell becoming visible and its first frame — stopped by
        # _show_title_overlay — so it never animates during steady playback.
        self._loading_pulse = QPropertyAnimation(self._overlay_effect, b"opacity", self)
        self._loading_pulse.setDuration(1100)
        self._loading_pulse.setStartValue(1.0)
        self._loading_pulse.setKeyValueAt(0.5, 0.4)
        self._loading_pulse.setEndValue(1.0)
        self._loading_pulse.setLoopCount(-1)
        self._loading_pulse.setEasingCurve(QEasingCurve.Type.InOutSine)

        self.setMouseTracking(True)
        self._sig_eof.connect(self._handle_eof, Qt.ConnectionType.QueuedConnection)
        self._sig_track_done.connect(
            self._handle_track_done, Qt.ConnectionType.QueuedConnection
        )
        self._sig_buffering.connect(
            self._handle_buffering, Qt.ConnectionType.QueuedConnection
        )

        # Stall watchdog: polls for silent freezes (frozen frame / wedged
        # decoder / network hang that survives reconnect) which never emit an
        # end-file or error event, so the retry chain would otherwise never fire.
        # Per-cell jitter on the interval desynchronizes the watchdogs — all
        # cells are constructed in one loop, and identical cadence means a
        # wall-wide stall gets flagged by every cell in the same tick.
        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.setInterval(
            WATCHDOG_INTERVAL_MS + random.randint(0, 1_000)
        )
        self._watchdog_timer.timeout.connect(self._check_stall)
        self._watchdog_timer.start()

    # ── mpv lifecycle ─────────────────────────────────────────────────────

    def _ensure_mpv(self) -> None:
        """Create the mpv instance if it doesn't exist.

        Must be called after the widget is visible and realized.
        """
        if self._mpv is not None:
            return

        import mpv as _mpv

        if not self.video_frame.isVisible():
            logger.warning("video_frame not visible — deferring mpv creation.")
            return

        _opts = self._mpv_opts or apply_env_overrides(MPV_OPTS)
        if sys.platform != "darwin":
            # HWND sign-extension fix: mask to 32-bit (Windows only — the
            # mask would corrupt a 64-bit pointer elsewhere).
            wid = native_wid(int(self.video_frame.winId()))
            if wid == 0:
                logger.warning("video_frame.winId() == 0 — widget not realized yet.")
                return

        # Suppress FFmpeg C-level stdout/stderr during creation
        _std_saved = (sys.stdout, sys.stderr)
        _devnull = open(os.devnull, "w")
        try:
            sys.stdout = sys.stderr = _devnull
            if sys.platform == "darwin":
                # No --wid on macOS (unsupported by mpv's Swift backend):
                # vo=libmpv (from the platform opts) renders through the
                # MpvGLWidget's GL framebuffer.
                m = _mpv.MPV(
                    log_handler=self._mpv_log,
                    **_opts,
                )
            else:
                m = _mpv.MPV(
                    wid=str(wid),
                    log_handler=self._mpv_log,
                    **_opts,
                )
        finally:
            sys.stdout, sys.stderr = _std_saved
            _devnull.close()

        if sys.platform == "darwin":
            # Render context must exist before the first loadfile hits the
            # VO (render.h); attach_mpv creates it now if GL is up, else at
            # initializeGL — which precedes the staggered first play().
            self.video_frame.attach_mpv(m)

        # Apply initial state
        try:
            m["mute"] = self.muted
        except Exception as e:
            logger.debug("mpv: failed to set initial mute: %s", e)
        # Lazy audio: muted cells load with aid=no. Beyond saving decode,
        # this is load-bearing for correctness — some real files are so
        # poorly interleaved that demuxing their audio stream at load drags
        # the whole cell into a paused-for-cache freeze (probed live on
        # demonmika_joi_2: aid=auto stalls at ~9s, aid=no plays clean).
        # v10.8 armed audio at load to make unmute seamless and reintroduced
        # exactly that freeze on the wall's *primary* passive-playback mode;
        # audio is (re)armed on first unmute instead — see _enable_audio_track.
        self._audio_started = not self.muted
        try:
            m["aid"] = "auto" if self._audio_started else "no"
        except Exception as e:
            logger.debug("mpv: failed to set initial aid: %s", e)
        if self.looping:
            try:
                m["loop-file"] = "inf"
            except Exception as e:
                logger.debug("mpv: failed to set initial loop-file: %s", e)

        self._mpv_gen += 1
        gen = self._mpv_gen

        @m.event_callback("end-file")
        def _on_end_file(ev: Any) -> None:
            self._sig_eof.emit(gen, end_file_reason(ev))

        # PRIMARY advance trigger. With keep_open="always" a naturally
        # finished track fires NO end-file event at all — mpv just pauses on
        # the last frame and flips eof-reached to True (probed live against
        # the shipped DLL, 2026-07-12). Advancement wired only to end-file
        # left every finished clip frozen until (at best) the stall watchdog.
        @m.property_observer("eof-reached")
        def _on_eof_reached(_name: str, value: Any) -> None:
            if value is True and gen == self._mpv_gen:
                self._sig_track_done.emit(gen)

        # Freeze visibility: mpv pauses itself when the demuxer cache
        # starves (network stall/reset). Bare emit per the observer rules.
        @m.property_observer("paused-for-cache")
        def _on_pfc(_name: str, value: Any) -> None:
            if value is not None and gen == self._mpv_gen:
                self._sig_buffering.emit(gen, bool(value))

        @m.property_observer("time-pos")
        def _on_time(_name: str, value: float | None) -> None:
            if value is None or gen != self._mpv_gen:
                return
            # Record forward progress for the stall watchdog. mpv can emit the
            # same time-pos repeatedly on a frozen stream; only a real advance
            # counts as "alive".
            if value > self._last_seen_pos:
                self._last_seen_pos = value
                self._last_progress_ts = _time.monotonic()
            self._play_pos = value
            if value > 0.02 and not self._played_anything:
                self._played_anything = True
            if self._duration_s > 0 and self._duration_s < 0.5 and value > 0:
                self._played_anything = True

        @m.property_observer("duration")
        def _on_dur(_name: str, value: float | None) -> None:
            if gen != self._mpv_gen:
                return
            if value:
                self._duration_s = float(value)

        if STATS_ENABLED:
            for _prop in STATS_COUNTER_PROPS:
                @m.property_observer(_prop)
                def _on_counter(
                    _name: str, value: float | None,
                    _gen: int = gen, _prop: str = _prop,
                ) -> None:
                    if _gen != self._mpv_gen or value is None:
                        return
                    self._stats_current[_prop] = float(value)

            for _prop in STATS_INFO_PROPS:
                @m.property_observer(_prop)
                def _on_info(
                    _name: str, value: Any,
                    _gen: int = gen, _prop: str = _prop,
                ) -> None:
                    if _gen != self._mpv_gen or value is None:
                        return
                    self._stats_info[_prop] = value

        self._mpv = m

    def _destroy_mpv(self, wait_s: float = 1.5) -> None:
        """Terminate mpv with a genuinely bounded wait.

        terminate runs on a daemon thread; we join for at most `wait_s` and
        then truly abandon it. The previous ThreadPoolExecutor version looked
        bounded but wasn't: exiting its `with` block calls shutdown(wait=True),
        which blocks the GUI thread until terminate returns — one wedged libmpv
        teardown froze every cell on the wall indefinitely.
        """
        if self._mpv is None:
            return
        if STATS_ENABLED:
            self._flush_stats()
        if sys.platform == "darwin":
            # render.h: the render context must be freed BEFORE the mpv core
            # is destroyed, with the GL context current (GUI thread here).
            self.video_frame.release()
        # Silence the handle BEFORE terminate: a wedged teardown gets
        # abandoned on a daemon thread below, and an abandoned-but-alive
        # instance that was audible would keep playing sound that no
        # control can reach (its cell now talks to the replacement).
        try:
            self._mpv["mute"] = True
        except Exception:
            pass
        mpv_ref = self._mpv
        self._mpv = None

        def _terminate() -> None:
            try:
                mpv_ref.terminate()
            except Exception as e:
                logger.debug("mpv terminate raised: %s", e)

        t = threading.Thread(
            target=_terminate, name="mpv-terminate", daemon=True
        )
        t.start()
        if wait_s > 0:
            t.join(wait_s)
            if t.is_alive():
                logger.warning(
                    "mpv terminate still running after %.1fs — abandoning it "
                    "on a daemon thread.", wait_s,
                )

    def _flush_stats(self) -> None:
        """Snapshot current mpv stats into running totals."""
        if self._mpv is not None:
            # Stats names are property-only: read via attribute (m[...] is
            # options/<name> and raises), or the snapshot silently no-ops and
            # the totals ride on whatever the observers last delivered.
            for prop in STATS_COUNTER_PROPS:
                try:
                    v = getattr(self._mpv, prop.replace("-", "_"))
                    if v is not None:
                        self._stats_current[prop] = float(v)
                except Exception:
                    pass
            for prop in STATS_INFO_PROPS:
                try:
                    v = getattr(self._mpv, prop.replace("-", "_"))
                    if v is not None:
                        self._stats_info[prop] = v
                except Exception:
                    pass
        # Detach-swap before iterating: the stats observers write these
        # dicts from the mpv event thread, and iterating a dict that grows
        # mid-iteration raises. The reference swap is GIL-atomic; observers
        # write to the fresh dict while we drain the detached one.
        current, self._stats_current = self._stats_current, {}
        for k, v in current.items():
            self._stats_total[k] = self._stats_total.get(k, 0.0) + v

    def _mpv_log(self, level: str, component: str, message: str) -> None:
        """Route mpv log messages to Python logging, suppressing noise."""
        text = message.strip()
        if level == "warn" and any(pat in text for pat in MPV_LOG_NOISE):
            return
        msg = f"mpv[{component}] {text}"
        if level in ("fatal", "error"):
            logger.error(msg)
        elif level == "warn":
            logger.warning(msg)

    # ── Qt events ─────────────────────────────────────────────────────────

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if sys.platform != "darwin":
            # Windows: force native window creation for the wid embed.
            # QOpenGLWidget needs no winId (and native-windowing it would
            # change compositing), so skip on macOS.
            self.video_frame.winId()
        if not self._played_anything and self.current_item is None:
            # Visual feedback while staggered startup loads content.
            self._show_loading()

    def hideEvent(self, event: Any) -> None:
        # Stop the looping LOADING pulse so a cell that never reached play()
        # (failed query, app closing mid-stagger) doesn't keep driving opacity
        # repaints forever. showEvent re-arms it if we're still content-less.
        self._loading_pulse.stop()
        super().hideEvent(event)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._reposition_controls()
        if self._title_overlay.isVisible():
            self._reposition_overlay()

    def enterEvent(self, event: Any) -> None:
        self._mouse_in_cell = True
        if not self.controls_visible:
            self._fade_controls(True)
            self.controls_frame.raise_()
            self.controls_visible = True
        self._autohide_timer.start(AUTOHIDE_MS)
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self._mouse_in_cell = False
        if self.controls_visible:
            self._autohide_timer.start(MOUSE_IDLE_MS)
        super().leaveEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        self._mouse_in_cell = True
        if not self.controls_visible:
            self._fade_controls(True)
            self.controls_frame.raise_()
            self.controls_visible = True
        self._autohide_timer.start(AUTOHIDE_MS)
        super().mouseMoveEvent(event)

    # ── playback ──────────────────────────────────────────────────────────

    def _begin_track(self, item: dict[str, Any]) -> None:
        """Per-track state + UI reset shared by play() and the prefetched
        advance. No title overlay here: the wall stays chrome-free during
        steady playback (title lives in the hover control bar)."""
        if self.current_item is not item:
            self._retry_count = 0
            self._force_transcode = False
        self.current_item = item
        self._duration_s = 0.0
        self._play_pos = 0.0
        # Reset stall tracking so the freshly-loaded track gets a full grace
        # window before the watchdog can flag it.
        self._last_seen_pos = -1.0
        self._last_progress_ts = _time.monotonic()

        self.lbl_title.setText(item.get("Name", "Unknown"))

        # Update tag/fav buttons. Emby serves applied tags under TagItems and
        # leaves Tags null, so read via the shared helper (item["Tags"] alone
        # shows an already-tagged clip as untagged).
        self.btn_tag.setChecked("ToDelete" in tag_names(item))
        self.btn_fav.setChecked(
            item.get("UserData", {}).get("IsFavorite", False)
        )
        # The LOADING pulse / error card used to be dismissed by the title
        # card that popped on every track change; with that chrome gone the
        # overlay must be dropped explicitly when a new track starts.
        self._close_open_freeze()
        self._hide_overlay()

    def _hide_overlay(self) -> None:
        """Drop any overlay card (LOADING pulse, error notice) immediately —
        a new track is starting and the wall stays chrome-free."""
        self._overlay_show_timer.stop()
        self._overlay_anim.stop()
        self._loading_pulse.stop()
        self._title_overlay.hide()

    @traced("cell.play")
    def play(self, item: dict[str, Any], url: str) -> None:
        """Load a video into this cell."""
        if self._parked:
            # A manual/web advance during the park cooldown is a deliberate
            # resume: clear the parked state so a failure of THIS load runs
            # the normal retry chain instead of being swallowed by the
            # _on_error parked-guard (2026-07-13 audit). The pending unpark
            # timer no-ops (it checks _parked).
            self._parked = False
            self._failure_ts.clear()
            logger.info("Parked cell manually resumed.")
        # loadfile (replace) clears the mpv playlist tail, so any queued
        # prefetch entry is gone with it (probed live 2026-07-13).
        self._prefetched = None
        self._is_transcoding = ".m3u8" in url  # transcode = HLS playlist URL
        self._begin_track(item)

        # Determine if we need to recreate mpv
        need_create = self._mpv is None or self._force_transcode
        if not need_create and self._mpv is not None:
            try:
                self._mpv["pause"]  # liveness check
            except Exception:
                logger.warning("mpv process dead — recreating.")
                need_create = True

        # Only reset _played_anything when creating a new mpv instance.
        # When reusing an existing mpv (loadfile replaces the current track),
        # mpv fires end-file for the old track before starting the new one.
        # Keeping _played_anything=True across the switch prevents that stale
        # end-file from being misclassified as a playback error. The time-pos
        # observer will re-assert True once the new track produces frames.
        if need_create:
            self._played_anything = False

        if need_create:
            self._destroy_mpv()
            self._ensure_mpv()
        elif self._mpv is not None:
            # Reusing the instance: loadfile resets mpv's per-file counters,
            # so bank the outgoing track's stats now or they're lost.
            if STATS_ENABLED:
                self._flush_stats()
            # New track: a re-muted cell drops back to aid=no so the next
            # file starts in the safe lazy-audio state (see _ensure_mpv).
            if self.muted and self._audio_started:
                try:
                    self._mpv["aid"] = "no"
                    self._audio_started = False
                except Exception as e:
                    logger.debug("mpv: failed to re-disable aid: %s", e)

        if self._mpv is None:
            logger.error("mpv not initialized — cannot play.")
            return

        # _switching suppresses stale events from the track loadfile replaces:
        # the old track's end-file (reason "stop") and any in-flight
        # eof-reached signal. Only a REUSED mpv has a track to replace — on a
        # fresh instance no stale event will ever arrive, and setting the flag
        # there latches it forever (watchdog + advance both gated on it).
        self._switching = not need_create
        self._track_done = False
        try:
            self._mpv["mute"] = self.muted
            self._mpv.command("loadfile", url)
            # keep-open pauses the player at EOF and the pause property
            # PERSISTS across loadfile (probed live 2026-07-12) — without an
            # explicit unpause every post-EOF load sits frozen on frame 0.
            self._mpv["pause"] = False
            self._paused = False
            self.btn_play.setText(_G_PAUSE)
        except Exception as e:
            self._switching = False
            logger.error("mpv loadfile failed: %s", e)
            self._sig_eof.emit(self._mpv_gen, "error")
            return

    # ── gapless prefetch ──────────────────────────────────────────────────

    def prefetch(self, item: dict[str, Any], url: str, session_id: str) -> bool:
        """Queue the next item on the live mpv playlist.

        With prefetch-playlist=yes, mpv opens the queued entry's demuxer as
        soon as the current one is fully read (≈ demuxer_readahead_secs
        before EOF), so the network stream is already warm when we advance —
        probed at ~60ms to first frame vs a cold loadfile's open latency.
        """
        if self._mpv is None:
            return False
        try:
            self._mpv.command("loadfile", url, "append")
        except Exception as e:
            logger.debug("prefetch append failed: %s", e)
            return False
        self._prefetched = (item, url, session_id)
        return True

    def drop_prefetch(self) -> None:
        """Forget the queued entry (e.g. the wall's filter changed and the
        drawn item may no longer belong). The mpv-side playlist entry is
        cleared by the next loadfile replace."""
        self._prefetched = None

    @traced("cell.advance_to_prefetched")
    def advance_to_prefetched(self) -> bool:
        """Jump to the prefetched playlist entry.

        Returns False when there is nothing usable (no queue, dead mpv,
        command failure) — the caller falls back to a cold play(). Mirrors
        play()'s reuse path: bank stats, drop a re-muted cell back to
        aid=no, arm the _switching guard for the old track's stale
        end-file (reason "stop", probed live), and explicitly unpause
        because the keep-open EOF pause persists across the switch.
        """
        if self._prefetched is None or self._mpv is None:
            return False
        item, _url, sid = self._prefetched
        self._prefetched = None
        self._is_transcoding = ".m3u8" in _url  # transcode = HLS playlist URL
        if STATS_ENABLED:
            self._flush_stats()
        if self.muted and self._audio_started:
            try:
                self._mpv["aid"] = "no"
                self._audio_started = False
            except Exception as e:
                logger.debug("mpv: failed to re-disable aid: %s", e)
        self._switching = True
        self._track_done = False
        try:
            self._mpv.command("playlist-next")
            self._mpv["pause"] = False
            self._paused = False
            self.btn_play.setText(_G_PAUSE)
        except Exception as e:
            self._switching = False
            logger.warning(
                "Prefetched advance failed (%s) — falling back to reload.", e
            )
            return False
        self._begin_track(item)
        self._emby_session_id = sid
        self._emby_item_id = item["Id"]
        return True

    def release(self) -> None:
        """Clean up and release all resources."""
        try:
            self._watchdog_timer.stop()
        except Exception:
            pass
        self._destroy_mpv()

    # ── controls UI ───────────────────────────────────────────────────────

    def _build_controls(self) -> None:
        self.controls_frame = QFrame(self)
        self.controls_frame.setObjectName("controls")
        self.controls_frame.setFixedHeight(CONTROLS_HEIGHT)
        self.controls_frame.setStyleSheet(CTRL_STYLE)

        self._ctrl_effect = QGraphicsOpacityEffect(self.controls_frame)
        self.controls_frame.setGraphicsEffect(self._ctrl_effect)
        self._ctrl_anim = QPropertyAnimation(self._ctrl_effect, b"opacity", self)
        self._ctrl_anim.setDuration(150)
        self._ctrl_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._ctrl_anim.finished.connect(self._on_ctrl_fade_done)
        self._ctrl_effect.setOpacity(CONTROLS_OPACITY)

        # Single-row floating pill: transport | seek + time | loop/audio | flags.
        # The title is no longer squatting in the bar — it shows as the hover
        # card (see _fade_controls) — which frees the stretch for a long,
        # precise seek slider.
        row = QHBoxLayout(self.controls_frame)
        row.setContentsMargins(_s(12), _s(4), _s(12), _s(4))
        row.setSpacing(_s(4))

        def _btn(text: str, tip: str, checkable: bool = False) -> QPushButton:
            b = QPushButton(text)
            b.setCheckable(checkable)
            b.setToolTip(tip)
            return b

        def _sep() -> QFrame:
            s = QFrame()
            s.setObjectName("ctrlSep")
            s.setFixedSize(1, _s(18))
            return s

        self.btn_prev = _btn(_G_PREV, "Previous")
        self.btn_play = _btn(_G_PAUSE, "Play / pause")
        self.btn_next = _btn(_G_NEXT, "Next")
        self.btn_loop = _btn(_G_LOOP, "Loop this video", checkable=True)
        self.btn_tag = _btn(_G_TRASH, "Flag for deletion", checkable=True)
        self.btn_fav = _btn(_G_FAV, "Favorite", checkable=True)
        self.btn_mute = _btn(_G_MUTE, "Mute / unmute", checkable=True)
        self.btn_mute.setChecked(True)
        # Named so their active (checked) state tints the glyph (gold / red)
        # instead of filling with accent — see the #favBtn:checked /
        # #tagBtn:checked rules in CTRL_STYLE.
        self.btn_fav.setObjectName("favBtn")
        self.btn_tag.setObjectName("tagBtn")
        # Audible cells tint the speaker glyph accent-bright (#muteBtn
        # [audible="true"] rule) so a glance at the wall shows exactly which
        # cells are live — audio is per-cell and several can play at once.
        self.btn_mute.setObjectName("muteBtn")
        self.btn_mute.setProperty("audible", False)

        self.seek_slider = ClickSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setFixedHeight(_s(14))
        self.seek_slider.sliderPressed.connect(self._seek_press)
        self.seek_slider.sliderReleased.connect(self._seek_release)

        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(0)
        self.vol_slider.setFixedWidth(_s(52))
        self.vol_slider.setFixedHeight(_s(14))
        self.vol_slider.setToolTip("Volume (drag to unmute)")
        # Always present. It was hidden while muted, but showing a child
        # dynamically under the pill's QGraphicsOpacityEffect doesn't repaint
        # reliably on the live wall (fine offscreen, where grab() forces a
        # full render) — and a static row is better anyway: nothing shifts,
        # and dragging up from 0 while muted already unmutes via
        # _vol_changed. At 0 the empty groove reads "silent" on its own.

        self.lbl_time = QLabel("0:00 / 0:00")
        self.lbl_time.setFixedWidth(_s(78))
        self.lbl_time.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        # Kept as the current-title store (play()/_begin_track write it; the
        # hover card reads it) — not part of the bar layout anymore.
        self.lbl_title = QLabel("Initializing…")
        self.lbl_title.hide()

        for w in (self.btn_prev, self.btn_play, self.btn_next):
            row.addWidget(w)
        row.addSpacing(_s(6))
        row.addWidget(self.seek_slider, stretch=1)
        row.addWidget(self.lbl_time)
        row.addSpacing(_s(6))
        row.addWidget(_sep())
        row.addWidget(self.btn_loop)
        row.addWidget(self.btn_mute)
        row.addWidget(self.vol_slider)
        row.addWidget(_sep())
        row.addWidget(self.btn_tag)
        row.addWidget(self.btn_fav)

        # Wire signals
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_prev.clicked.connect(lambda: self.request_prev.emit(self))
        self.btn_next.clicked.connect(lambda: self.request_next.emit(self, False))
        self.btn_loop.clicked.connect(self._toggle_loop)
        self.btn_tag.clicked.connect(self._toggle_tag)
        self.btn_fav.clicked.connect(self._toggle_fav)
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.vol_slider.valueChanged.connect(self._vol_changed)
        self.vol_slider.sliderReleased.connect(self._record_resting_vol)

    # ── control visibility ────────────────────────────────────────────────

    def set_controls_visible(self, visible: bool) -> None:
        self.controls_visible = visible
        if visible:
            self._fade_controls(True)
            self.controls_frame.raise_()
            self._autohide_timer.start(AUTOHIDE_MS)
        else:
            self._autohide_timer.stop()
            self._fade_controls(False)

    @traced("cell._fade_controls")
    def _fade_controls(self, visible: bool) -> None:
        self._ctrl_anim.stop()
        if visible:
            self.controls_frame.setVisible(True)
            if not self._ui_timer.isActive():
                self._refresh_progress_ui()
                self._ui_timer.start()
            # The bar no longer carries the title — flash the hover card
            # instead (auto-fades). LOADING/error cards keep priority.
            if self.current_item and not self._title_overlay.isVisible():
                self._show_title_overlay(self.lbl_title.text())
        self._ctrl_anim.setStartValue(self._ctrl_effect.opacity())
        self._ctrl_anim.setEndValue(CONTROLS_OPACITY if visible else 0.0)
        self._ctrl_anim.start()

    def _on_ctrl_fade_done(self) -> None:
        if self._ctrl_effect.opacity() < 0.01:
            self.controls_frame.setVisible(False)
            self._ui_timer.stop()

    def _autohide_controls(self) -> None:
        if self._mouse_in_cell:
            self._autohide_timer.start(AUTOHIDE_MS)
            return
        # Only this cell's state: the controller flag is the GLOBAL toggle
        # (C key / web). One cell's autohide used to clear it wall-wide,
        # desyncing the global toggle and the web status (2026-07-13 audit).
        self.controls_visible = False
        self._fade_controls(False)

    def _reposition_controls(self) -> None:
        if hasattr(self, "controls_frame"):
            h = self.controls_frame.height()
            m = _s(12)  # float the pill off the cell edges
            self.controls_frame.setGeometry(
                m, self.height() - h - m, self.width() - 2 * m, h,
            )
            self.controls_frame.raise_()

    # ── title overlay ─────────────────────────────────────────────────────

    def _show_title_overlay(self, title: str, sticky: bool = False) -> None:
        """Show the overlay card. sticky=True keeps it up (no auto-fade) —
        used by the parked card, which previously faded after 3s and left a
        black frozen cell unexplained for the rest of the 120s cooldown."""
        self._overlay_show_timer.stop()
        self._overlay_anim.stop()
        self._loading_pulse.stop()
        self._title_overlay.setStyleSheet(_TITLE_STYLE)
        self._title_overlay.setText(title)
        self._overlay_effect.setOpacity(1.0)
        self._title_overlay.adjustSize()
        self._reposition_overlay()
        self._title_overlay.show()
        self._title_overlay.raise_()
        if not sticky:
            self._overlay_show_timer.start(OVERLAY_SHOW_MS)

    def _show_loading(self, text: str = "LOADING") -> None:
        """Show a pulsing status card (LOADING at startup, BUFFERING during
        a cache starvation). Does not auto-fade — it stays (pulsing) until
        the condition clears, so a stalled cell reads as busy-with-reason
        rather than silently frozen.
        """
        self._overlay_show_timer.stop()
        self._overlay_anim.stop()
        self._title_overlay.setStyleSheet(_LOADING_STYLE)
        self._title_overlay.setText(text)
        self._title_overlay.adjustSize()
        self._reposition_overlay()
        self._title_overlay.show()
        self._title_overlay.raise_()
        self._overlay_effect.setOpacity(1.0)
        self._loading_pulse.start()  # start() implicitly stops any prior run

    def _reposition_overlay(self) -> None:
        vw = self.video_frame
        ovl = self._title_overlay
        ovl.adjustSize()
        w = min(ovl.sizeHint().width(), max(vw.width() - 24, 0))
        h = ovl.sizeHint().height()
        x = vw.x() + (vw.width() - w) // 2
        # Clear the floating control pill so the card never sits under it.
        y = vw.y() + vw.height() - h - CONTROLS_HEIGHT - _s(24)
        ovl.setFixedWidth(w)
        ovl.move(x, y)

    def _fade_overlay_out(self) -> None:
        self._overlay_anim.setStartValue(1.0)
        self._overlay_anim.setEndValue(0.0)
        self._overlay_anim.start()

    def _on_overlay_fade_done(self) -> None:
        if self._overlay_effect.opacity() < 0.01:
            self._title_overlay.hide()

    # ── playback control helpers ──────────────────────────────────────────

    @staticmethod
    def _fmt_time(s: float) -> str:
        s = max(0, int(s))
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _refresh_progress_ui(self) -> None:
        pos, dur = self._play_pos, self._duration_s
        if not self._dragging and dur > 0:
            self.seek_slider.setValue(int(pos / dur * 1000))
        self.lbl_time.setText(f"{self._fmt_time(pos)} / {self._fmt_time(dur)}")

    @traced("cell._seek_press")
    def _seek_press(self) -> None:
        self._dragging = True
        self._autohide_timer.stop()
        # Remember the pre-drag pause state: releasing a seek used to
        # unconditionally resume, silently un-pausing a deliberately
        # paused cell (2026-07-13 audit).
        self._paused_before_seek = self._paused
        if self._mpv is not None:
            try:
                self._mpv["pause"] = True
                self._paused = True
            except Exception:
                pass

    @traced("cell._seek_release")
    def _seek_release(self) -> None:
        if self._mpv is not None and self._duration_s > 0:
            try:
                # 0.98, not 0.90: the property-driven EOF advance makes the
                # clip tail safe to seek into; 10% of every video was
                # unreachable for no remaining reason.
                frac = min(self.seek_slider.value() / 1000.0, 0.98)
                target = frac * self._duration_s
                self._mpv.seek(target, "absolute")
                # Restore the PRE-DRAG pause state instead of always
                # resuming — a paused cell stays paused after a seek.
                resume_paused = getattr(self, "_paused_before_seek", False)
                self._last_seek_ts = _time.monotonic()
                self._mpv["pause"] = resume_paused
                self._paused = resume_paused
                self.btn_play.setText(_G_PLAY if resume_paused else _G_PAUSE)
            except Exception as e:
                logger.warning("seek failed: %s", e)
        self._dragging = False

    def set_paused_ui(self, paused: bool) -> None:
        """Sync the play/pause button glyph to an externally-set pause state
        (global pause toggle, web remote) using the VS15 monochrome glyphs."""
        self.btn_play.setText(_G_PLAY if paused else _G_PAUSE)
        self._nudge_pill()

    def _nudge_pill(self) -> None:
        """Repaint the whole control pill. Targeted child updates stale
        under its QGraphicsOpacityEffect on the live wall (confirmed on the
        volume slider) — every programmatic control-state change routes a
        full-pill update through here."""
        self.controls_frame.update()

    @traced("cell._toggle_play")
    def _toggle_play(self) -> None:
        if self._mpv is None:
            return
        try:
            new_pause = not bool(self._mpv["pause"])
            self._mpv["pause"] = new_pause
            self._paused = new_pause
            self.btn_play.setText(_G_PLAY if new_pause else _G_PAUSE)
            self._nudge_pill()
        except Exception as e:
            logger.debug("toggle_play failed: %s", e)

    def _toggle_loop(self) -> None:
        self.looping = self.btn_loop.isChecked()
        self._nudge_pill()
        if self._mpv is not None:
            try:
                self._mpv["loop-file"] = "inf" if self.looping else "no"
            except Exception as e:
                logger.debug("toggle_loop failed: %s", e)

    def _enable_audio_track(self) -> None:
        """Arm this track's audio the first time the cell is unmuted.

        Muted cells load aid=no (see _ensure_mpv) — safe for poorly
        interleaved files. Selecting the track cold under video_sync=audio
        would stutter until the buffer fills, so we relock with a seek. The
        relock is a KEYFRAME seek, not an exact one: exact re-decodes from
        the last keyframe to the current position (the ~1s freeze owners
        reported), while keyframe jumps to the nearest keyframe fast and
        still flushes/refills both decoders. Probed across the library:
        keyframe starts audio cleanly with ≤4 sample stalls where no-seek
        stuttered up to 12; exact worked but froze. No-op once armed.
        """
        if self._audio_started or self._mpv is None:
            return
        try:
            t0 = _time.perf_counter()
            self._mpv["aid"] = "auto"
            aid_ms = (_time.perf_counter() - t0) * 1000
            self._audio_started = True
            # time-pos is maintained by the observer on the mpv event thread.
            # Reading m.time_pos here is synchronous libmpv IPC and was part
            # of the 210ms GUI click stall caught on the M5 soak.  The cached
            # value is fresh at video-frame cadence and a keyframe relock is
            # tolerant of a slightly stale position.
            pos = self._play_pos if self._play_pos > 0 else None
            seek_ms = 0.0
            if pos is not None:
                t0 = _time.perf_counter()
                self._mpv.seek(pos, "absolute+keyframes")
                seek_ms = (_time.perf_counter() - t0) * 1000
            logger.info(
                "AUDIO arm: aid=%.0fms seek=%.0fms cached-pos=%s",
                aid_ms, seek_ms, "yes" if pos is not None else "no",
            )
        except Exception as e:
            # WARNING, not debug: _apply_mute proceeds to show the audible
            # glow, so a swallowed failure here = a "live" cell with no
            # sound and no trace (2026-07-13 audit).
            logger.warning("Audio track arm failed on unmute: %s", e)

    def _sync_mute_ui(self, muted: bool) -> None:
        """Single writer for every mute-state visual: glyph, checked state,
        and the audible accent tint. Three call sites used to update these
        piecemeal — the exact shape that lets button state drift from
        player state."""
        self.btn_mute.setChecked(muted)
        self.btn_mute.setText(_G_MUTE if muted else _G_UNMUTE)
        self.btn_mute.setProperty("audible", not muted)
        st = self.btn_mute.style()
        st.unpolish(self.btn_mute)
        st.polish(self.btn_mute)
        self._nudge_pill()

    def _apply_mute(self, muted: bool) -> None:
        """Single writer for the mute state itself (cache + mpv + UI).

        Unmuting arms the audio track first (lazy — see _enable_audio_track),
        then clears mpv's mute flag."""
        self.muted = muted
        if not muted:
            self._enable_audio_track()
        if self._mpv is not None:
            try:
                self._mpv["mute"] = muted
            except Exception as e:
                logger.debug("apply_mute failed: %s", e)
        self._sync_mute_ui(muted)

    @traced("cell._toggle_mute")
    def _toggle_mute(self) -> None:
        muted = self.btn_mute.isChecked()
        self._apply_mute(muted)
        # Unmuting must land somewhere audible: restore this cell's last
        # used volume (default 70). The old ==0 check missed sliders left
        # at low nonzero values from earlier fiddling.
        if not muted and self.vol_slider.value() < 10:
            self.vol_slider.setValue(self._last_vol)

    def _record_resting_vol(self) -> None:
        """Remember where a volume drag ENDED (sliderReleased)."""
        if self.vol_slider.value() >= 10:
            self._last_vol = self.vol_slider.value()

    def _vol_changed(self, val: int) -> None:
        # Remember deliberate resting volumes only. Mid-drag samples must
        # not count: dragging DOWN from 70 sweeps every value ≥10 past this
        # handler, which left _last_vol ≈ 10 and made the next unmute
        # "restore" to a whisper (owner-reported). Drag endpoints land via
        # _record_resting_vol; this guard covers non-drag changes (clicks,
        # the unmute restore itself is excluded by isSliderDown()=False but
        # harmlessly re-records its own value).
        if val >= 10 and not self.vol_slider.isSliderDown():
            self._last_vol = val
        if self._mpv is not None:
            try:
                self._mpv["volume"] = float(val)
            except Exception as e:
                logger.debug("vol_changed failed: %s", e)
        if val > 0 and self.muted:
            self._apply_mute(False)   # drag up from silence = unmute
        elif val == 0 and not self.muted:
            self._apply_mute(True)    # drag to zero = mute

    @traced("cell._toggle_tag")
    def _toggle_tag(self) -> None:
        if not self.current_item:
            return
        self._nudge_pill()
        # Read via the helper (Emby puts tags in TagItems, leaves Tags null —
        # the old item["Tags"] read hit list(None) on a library-loaded clip).
        tags = tag_names(self.current_item)
        if "ToDelete" in tags:
            tags.remove("ToDelete")
        else:
            tags.append("ToDelete")
        # Keep BOTH shapes in the local dict in sync so the helper (which
        # prefers TagItems) reflects the new state on the next read.
        self.current_item["Tags"] = tags
        self.current_item["TagItems"] = [{"Name": t} for t in tags]
        self.btn_tag.setChecked("ToDelete" in tags)  # :checked tints the glyph red
        self.controller.update_tags(self.current_item)

    @traced("cell._toggle_fav")
    def _toggle_fav(self) -> None:
        if not self.current_item:
            return
        self._nudge_pill()
        new = self.btn_fav.isChecked()  # :checked tints the glyph gold
        self.current_item.setdefault("UserData", {})["IsFavorite"] = new
        self.controller.update_favorite(self.current_item["Id"], new)

    # ── EOF / error handling ──────────────────────────────────────────────

    def _handle_buffering(self, gen: int, buffering: bool) -> None:
        """GUI-thread side of the paused-for-cache observer.

        Turns invisible network-starvation freezes into: a pulsing
        BUFFERING card on the cell, a WARNING log with the measured
        duration, and per-cell counters that ride the stats dump.
        """
        if gen != self._mpv_gen or self._mpv is None:
            return
        if buffering:
            if not self._played_anything:
                return  # startup fill, not a mid-playback freeze
            if self._freeze_t0 == 0.0:
                now = _time.monotonic()
                self._freeze_t0 = now
                # A refill right after a seek is expected demuxer behavior,
                # not a spontaneous starvation — count it separately so soak
                # numbers stop conflating the two (2026-07-14 soak: 43 random
                # seeks inflated the freeze count).
                if now - self._last_seek_ts < 5.0:
                    self._freeze_postseek_count += 1
                else:
                    self._freeze_count += 1
            # Replace whatever card is up (stale title / loading): during a
            # freeze the buffering state is the most relevant thing on the
            # cell. Parked/error cells never reach here (not playing).
            self._buffering_card = True
            self._show_loading("BUFFERING")
        else:
            if self._freeze_t0:
                dur = _time.monotonic() - self._freeze_t0
                self._freeze_total_s += dur
                self._freeze_t0 = 0.0
                try:
                    state = self._mpv.cache_buffering_state
                except Exception:
                    state = "?"
                tag = (
                    "post-seek refill"
                    if self._last_seek_ts > 0
                    and _time.monotonic() - self._last_seek_ts < 5.0 + dur
                    else "cache starvation"
                )
                logger.warning(
                    "FREEZE: %.1fs %s on '%s' (buffering-state=%s)",
                    dur, tag,
                    (self.current_item or {}).get("Name", "?"), state,
                )
            if self._buffering_card:
                self._buffering_card = False
                self._hide_overlay()

    def _close_open_freeze(self) -> None:
        """Bank a freeze still open when the track changes underneath it."""
        if self._freeze_t0:
            dur = _time.monotonic() - self._freeze_t0
            self._freeze_total_s += dur
            self._freeze_t0 = 0.0
            logger.warning(
                "FREEZE: %.1fs cache starvation (ended by track change)", dur,
            )
        self._buffering_card = False

    @traced("cell._handle_track_done")
    def _handle_track_done(self, gen: int) -> None:
        """A track finished naturally (eof-reached flipped True).

        This is the wall's primary advance path: with keep_open="always" mpv
        emits NO end-file at natural EOF — it pauses on the last frame and
        flips the eof-reached property. The signal is queued from the mpv
        event thread, so re-check liveness against the CURRENT player state:
        a stale signal from a track that play() has since replaced must not
        advance the new one.
        """
        if gen != self._mpv_gen or self._mpv is None:
            return
        if self._paused:
            # Explicitly paused (global pause / user) — don't yank the wall
            # forward underneath a pause. The resume path re-checks
            # eof-reached and advances then (wall._global_toggle_pause).
            return
        if self._switching or self._track_done:
            return
        try:
            # Attribute access, NOT item access: python-mpv's m[...] reads
            # options/<name>, and eof-reached is property-only, so the old
            # m["eof-reached"] raised on every call and this guard silently
            # swallowed EVERY natural-EOF advance (cell froze on the last
            # frame until the stall watchdog "rescued" it as an error).
            if self._mpv.eof_reached is not True:
                return  # stale — the track this signal was about is gone
        except Exception:
            return
        if not self._played_anything:
            logger.warning("Track ended before first frame — treating as error.")
            self._track_done = True
            self._on_error()
            return
        if self.looping:
            try:
                self._mpv.seek(0, "absolute")
                self._mpv["pause"] = False
                self._paused = False
                return
            except Exception as e:
                logger.warning("Loop seek failed: %s", e)
        self._track_done = True
        logger.info(
            "Track finished: %s — advancing.",
            (self.current_item or {}).get("Name", "?"),
        )
        self._request_next_throttled(False)

    def _handle_eof(self, gen: int, reason: str) -> None:
        if gen != self._mpv_gen:
            return
        if reason in ("stop", "quit", "redirect", "restarted"):
            # "stop" is the stale end-file from a loadfile replace (and quit/
            # redirect are never an advance either). With the old broken
            # reason extraction these all read as "eof" and needed the
            # _switching dance to avoid phantom advances.
            self._switching = False
            return
        if reason == "error":
            # Clear the switching guard here too: on the error path it would
            # leak True, gating the new track's eof handling forever.
            self._switching = False
            self._on_error()
            return
        # reason == "eof": under keep_open="always" this doesn't fire at
        # natural EOF (eof-reached handles that above); kept as a fallback
        # for stream types that do emit it.
        if self._switching:
            self._switching = False
            return
        if self._track_done:
            return  # eof-reached already advanced this track
        if not self._played_anything:
            logger.warning("EOF before first frame — treating as error.")
            self._on_error()
            return
        if self.looping and self._mpv is not None:
            try:
                self._mpv.seek(0, "absolute")
                self._mpv["pause"] = False
                self._paused = False
            except Exception as e:
                logger.warning("Loop seek failed: %s", e)
                self._request_next_throttled(False)
        else:
            self._track_done = True
            self._request_next_throttled(False)

    def _request_next_throttled(self, is_retry: bool) -> None:
        MIN_INTERVAL = 0.75
        now = _time.monotonic()
        elapsed = now - self._last_next_request_ts
        if not is_retry and elapsed < MIN_INTERVAL:
            # Defer instead of dropping: a dropped EOF advance used to leave
            # the cell frozen on its last frame until the stall watchdog
            # rescued it 20s later. One pending advance at a time.
            if not self._pending_next:
                self._pending_next = True
                delay_ms = int((MIN_INTERVAL - elapsed) * 1000) + 50
                logger.warning(
                    "next_video throttled (last fire %.2fs ago) — "
                    "deferring %dms", elapsed, delay_ms,
                )
                QTimer.singleShot(delay_ms, self._fire_pending_next)
            return
        self._pending_next = False
        self._last_next_request_ts = now
        self.request_next.emit(self, is_retry)

    def _fire_pending_next(self) -> None:
        if not self._pending_next:
            return
        self._pending_next = False
        self._request_next_throttled(False)

    def _check_stall(self) -> None:
        """Watchdog: flag a silently frozen stream as an error.

        Runs every WATCHDOG_INTERVAL_MS. A cell that is playing (mpv alive, not
        paused, not being seeked) but whose time-pos hasn't advanced for
        STALL_TIMEOUT_S is treated as a playback error, reusing the existing
        retry/escalation chain — no new failure semantics.

        Deliberately does NOT skip on _switching: play() resets the progress
        timestamp (full grace window), so a load that silently wedges still
        gets rescued instead of the guard flag disabling the safety net.
        """
        if self._mpv is None or self._parked:
            return
        if not self._played_anything:
            # Startup / EOF-before-first-frame is handled by the normal path.
            return
        idle_s = _time.monotonic() - self._last_progress_ts
        if is_stalled(
            idle_s,
            paused=self._paused,
            dragging=self._dragging,
            threshold_s=STALL_TIMEOUT_S,
        ):
            logger.warning(
                "Stall detected — no progress for %.0fs (threshold %ds). "
                "Treating as error.", idle_s, STALL_TIMEOUT_S,
            )
            # Force a fresh grace window so we don't re-fire before the retry
            # has a chance to load new frames.
            self._last_progress_ts = _time.monotonic()
            self._on_error()

    def _record_failure_and_maybe_park(self) -> bool:
        """Record a failure timestamp; park the cell on a crash-loop.

        Returns True if the cell is now parked (caller should stop retrying).
        """
        now = _time.monotonic()
        self._failure_ts.append(now)
        if should_park(
            self._failure_ts, now,
            window_s=CRASH_LOOP_WINDOW_S,
            threshold=CRASH_LOOP_THRESHOLD,
        ):
            self._parked = True
            logger.error(
                "Crash-loop guard: %d failures within %ds — parking cell. "
                "Retrying in %ds.",
                CRASH_LOOP_THRESHOLD, CRASH_LOOP_WINDOW_S, CRASH_LOOP_COOLDOWN_S,
            )
            self._show_title_overlay(
                "Media unavailable — retrying soon…", sticky=True,
            )
            QTimer.singleShot(CRASH_LOOP_COOLDOWN_S * 1000, self._unpark)
            return True
        return False

    def _unpark(self) -> None:
        """Leave the parked state after the cooldown and try to resume."""
        if not self._parked:
            return
        self._parked = False
        self._failure_ts.clear()
        self._retry_count = 0
        self._force_transcode = False
        logger.info("Crash-loop cooldown elapsed — resuming cell.")
        self._request_next_throttled(False)

    def _on_error(self) -> None:
        if self._parked:
            return
        # Crash-loop guard: if failures pile up in a short window, stop
        # hammering Emby and park the cell instead.
        if self._record_failure_and_maybe_park():
            return
        # Systemic-outage check: when most of the wall is failing at once the
        # cause is shared (server/network), so a per-cell transcode escalation
        # only adds load to a struggling server. Back off longer instead.
        outage = False
        try:
            outage = self.controller.register_failure(self)
        except Exception as e:
            logger.debug("register_failure failed: %s", e)
        self._retry_count += 1
        logger.warning(
            "Playback error (attempt %d/%d)", self._retry_count, MAX_RETRIES
        )
        if outage:
            delay_s = apply_jitter(OUTAGE_BACKOFF_S, random.random())
            logger.warning(
                "Systemic outage suspected — backing off %.1fs without "
                "transcode escalation.", delay_s,
            )
            QTimer.singleShot(
                int(delay_s * 1000),
                lambda: self._request_next_throttled(True),
            )
            return
        plan = escalation_plan(self._retry_count, MAX_RETRIES)
        if plan["action"] == "retry":
            if plan["transcode"] and not self._force_transcode:
                self._force_transcode = True
                logger.info("Escalating to server transcode.")
            # Jitter desynchronizes retries: identical deterministic delays
            # made every cell hit the server at the same instant after a
            # wall-wide fault.
            delay_s = apply_jitter(plan["delay_s"], random.random())
            QTimer.singleShot(
                int(delay_s * 1000),
                lambda: self._request_next_throttled(True),
            )
        else:
            logger.error("Max retries reached — skipping.")
            self._force_transcode = False
            self._request_next_throttled(False)
