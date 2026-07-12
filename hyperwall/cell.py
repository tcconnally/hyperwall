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
)
from .reliability import (
    apply_jitter,
    end_file_reason,
    escalation_plan,
    is_stalled,
    should_park,
)
from . import theme

logger = logging.getLogger("HyperWall")


# Glassy translucent control bar, on-brand accent, rounded top. Buttons are
# borderless and quiet at rest, lifting to the accent on hover/active so the
# bar reads as one slab instead of a row of grey chips.
CTRL_STYLE = f"""
    QFrame#controls {{
        background: {theme.rgba(theme.SURFACE_0, 0.86)};
        border-top: 1px solid {theme.rgba(theme.ACCENT_BRIGHT, 0.22)};
        border-radius: {_s(8)}px {_s(8)}px 0 0;
    }}
    QLabel {{ color: {theme.TEXT_DIM}; font-family: {theme.FONT}; font-size: {_s(9)}px; background: transparent; }}
    QPushButton {{
        background: {theme.rgba('#ffffff', 0.06)}; border: none;
        border-radius: {_s(5)}px; color: {theme.TEXT}; font-size: {_s(11)}px; padding: 1px;
        min-width: {_s(24)}px; min-height: {_s(24)}px; max-width: {_s(24)}px; max-height: {_s(24)}px;
    }}
    QPushButton:hover   {{ background: {theme.ACCENT}; color: white; }}
    QPushButton:pressed {{ background: {theme.ACCENT_DEEP}; }}
    QPushButton:checked {{ background: {theme.ACCENT_DIM}; color: white; }}
    /* Favorite / trash: active state keeps the neutral bar fill and tints the
       monochrome glyph itself (gold / red) via QSS — reliable regardless of
       whether the platform font honours a VS16 colour-emoji selector. */
    QPushButton#favBtn:checked {{ background: {theme.rgba('#ffffff', 0.06)}; color: {theme.FAVORITE}; }}
    QPushButton#tagBtn:checked {{ background: {theme.rgba('#ffffff', 0.06)}; color: {theme.DANGER}; }}
    QSlider::groove:horizontal {{ background: {theme.rgba('#ffffff', 0.16)}; height: {_s(3)}px; border-radius: {_s(2)}px; }}
    QSlider::sub-page:horizontal {{ background: {theme.ACCENT}; border-radius: {_s(2)}px; }}
    QSlider::handle:horizontal {{
        background: #ffffff; width: {_s(9)}px; height: {_s(9)}px; margin: {_s(-3)}px 0; border-radius: {_s(5)}px;
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
    """Slider that jumps to click position."""

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderPosition(
                QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(),
                    int(event.position().x()), self.width(),
                )
            )
        super().mousePressEvent(event)


class VideoCell(QWidget):
    """A single video cell in the wall grid."""

    request_next = pyqtSignal(object, bool)
    request_prev = pyqtSignal(object)
    _sig_eof = pyqtSignal(int, str)
    _sig_track_done = pyqtSignal(int)

    def __init__(self, controller: Any):
        super().__init__()
        self.controller = controller
        self.current_item: dict[str, Any] | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=50)
        self.looping = False
        self.muted = True
        self.controls_visible = True

        # Internal state
        self._mpv: Any = None          # mpv.MPV instance
        self._mpv_gen = 0              # generation counter
        self._duration_s = 0.0
        self._play_pos = 0.0
        self._dragging = False
        self._retry_count = 0
        self._force_transcode = False
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
        self._audio_started = False  # True once an audio track has been selected

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

        # Autohide timer
        self._autohide_timer = QTimer(self)
        self._autohide_timer.setSingleShot(True)
        self._autohide_timer.timeout.connect(self._autohide_controls)
        self._autohide_timer.start(AUTOHIDE_MS)

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

        # HWND sign-extension fix: mask to 32-bit
        wid = int(self.video_frame.winId()) & 0xFFFFFFFF
        if wid == 0:
            logger.warning("video_frame.winId() == 0 — widget not realized yet.")
            return

        # Suppress FFmpeg C-level stdout/stderr during creation
        _std_saved = (sys.stdout, sys.stderr)
        _devnull = open(os.devnull, "w")
        try:
            sys.stdout = sys.stderr = _devnull
            _opts = self._mpv_opts or apply_env_overrides(MPV_OPTS)
            m = _mpv.MPV(
                wid=str(wid),
                log_handler=self._mpv_log,
                **_opts,
            )
        finally:
            sys.stdout, sys.stderr = _std_saved
            _devnull.close()

        # Apply initial state
        try:
            m["mute"] = self.muted
        except Exception as e:
            logger.debug("mpv: failed to set initial mute: %s", e)
        # Don't decode an audio track for a muted cell (the wall's default):
        # aid=no skips audio demux+decode for that cell, which adds up across
        # the grid. The track is (re)selected lazily on first unmute via
        # _enable_audio_track(), so no reload is needed to hear a cell.
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
            for prop in STATS_COUNTER_PROPS:
                try:
                    v = self._mpv[prop]
                    if v is not None:
                        self._stats_current[prop] = float(v)
                except Exception:
                    pass
            for prop in STATS_INFO_PROPS:
                try:
                    v = self._mpv[prop]
                    if v is not None:
                        self._stats_info[prop] = v
                except Exception:
                    pass
        for k, v in self._stats_current.items():
            self._stats_total[k] = self._stats_total.get(k, 0.0) + v
        self._stats_current.clear()

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
        self.video_frame.winId()  # force native window creation
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

    def play(self, item: dict[str, Any], url: str) -> None:
        """Load a video into this cell."""
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

        title = item.get("Name", "Unknown")
        self.lbl_title.setText(title)
        self._show_title_overlay(title)

        # Update tag/fav buttons
        raw = item.get("Tags", [])
        tag_names = (
            [t.get("Name", "") for t in raw]
            if raw and isinstance(raw[0], dict)
            else list(raw)
        )
        self.btn_tag.setChecked("ToDelete" in tag_names)
        self.btn_fav.setChecked(
            item.get("UserData", {}).get("IsFavorite", False)
        )

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
            # A cell that was unmuted and re-muted still has aid=auto and
            # decodes audio it never plays. The new file starts a fresh
            # decoder anyway, so this is the blip-free moment to drop back
            # to aid=no (mirrors the lazy re-enable in _enable_audio_track).
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

        outer = QVBoxLayout(self.controls_frame)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(1)

        self.seek_slider = ClickSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setFixedHeight(_s(10))
        self.seek_slider.sliderPressed.connect(self._seek_press)
        self.seek_slider.sliderReleased.connect(self._seek_release)
        outer.addWidget(self.seek_slider)

        row = QHBoxLayout()
        row.setSpacing(2)
        row.setContentsMargins(0, 0, 0, 0)

        def _btn(text: str, checkable: bool = False) -> QPushButton:
            b = QPushButton(text)
            b.setCheckable(checkable)
            return b

        self.btn_prev = _btn(_G_PREV)
        self.btn_play = _btn(_G_PAUSE)
        self.btn_next = _btn(_G_NEXT)
        self.btn_loop = _btn(_G_LOOP, checkable=True)
        self.btn_tag = _btn(_G_TRASH, checkable=True)
        self.btn_fav = _btn(_G_FAV, checkable=True)
        self.btn_mute = _btn(_G_MUTE, checkable=True)
        self.btn_mute.setChecked(True)
        # Named so their active (checked) state keeps a neutral bar-tone fill and
        # tints the glyph (gold / red) instead of filling with accent — see the
        # #favBtn:checked / #tagBtn:checked rules in CTRL_STYLE.
        self.btn_fav.setObjectName("favBtn")
        self.btn_tag.setObjectName("tagBtn")

        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(0)
        self.vol_slider.setFixedWidth(_s(45))
        self.vol_slider.setFixedHeight(_s(10))

        self.lbl_time = QLabel("0:00 / 0:00")
        self.lbl_time.setFixedWidth(_s(75))
        self.lbl_time.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        self.lbl_title = QLabel("Initializing…")
        self.lbl_title.setStyleSheet(
            f"color: white; font-family: 'Segoe UI', system-ui, sans-serif;"
            f" font-size: {_s(12)}px; font-weight: 700; background: transparent;"
        )

        for w in (
            self.btn_prev, self.btn_play, self.btn_next, self.btn_loop,
            self.btn_tag, self.btn_fav, self.btn_mute,
        ):
            row.addWidget(w)
        row.addSpacing(2)
        row.addWidget(self.vol_slider)
        row.addSpacing(4)
        row.addWidget(self.lbl_time)
        row.addSpacing(2)
        row.addWidget(self.lbl_title, stretch=1)
        outer.addLayout(row)

        # Wire signals
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_prev.clicked.connect(lambda: self.request_prev.emit(self))
        self.btn_next.clicked.connect(lambda: self.request_next.emit(self, False))
        self.btn_loop.clicked.connect(self._toggle_loop)
        self.btn_tag.clicked.connect(self._toggle_tag)
        self.btn_fav.clicked.connect(self._toggle_fav)
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.vol_slider.valueChanged.connect(self._vol_changed)

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

    def _fade_controls(self, visible: bool) -> None:
        self._ctrl_anim.stop()
        if visible:
            self.controls_frame.setVisible(True)
            if not self._ui_timer.isActive():
                self._refresh_progress_ui()
                self._ui_timer.start()
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
        self.controls_visible = False
        self.controller.controls_visible = False
        self._fade_controls(False)

    def _reposition_controls(self) -> None:
        if hasattr(self, "controls_frame"):
            h = self.controls_frame.height()
            self.controls_frame.setGeometry(0, self.height() - h, self.width(), h)
            self.controls_frame.raise_()

    # ── title overlay ─────────────────────────────────────────────────────

    def _show_title_overlay(self, title: str) -> None:
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
        self._overlay_show_timer.start(OVERLAY_SHOW_MS)

    def _show_loading(self) -> None:
        """Show a pulsing 'LOADING' card until the first frame arrives.

        Unlike the title card this does not auto-fade — it stays (pulsing)
        until play() swaps in the real title, so a slow-loading cell still
        reads as busy rather than blank.
        """
        self._overlay_show_timer.stop()
        self._overlay_anim.stop()
        self._title_overlay.setStyleSheet(_LOADING_STYLE)
        self._title_overlay.setText("LOADING")
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
        y = vw.y() + vw.height() - h - 20
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

    def _seek_press(self) -> None:
        self._dragging = True
        self._autohide_timer.stop()
        if self._mpv is not None:
            try:
                self._mpv["pause"] = True
                self._paused = True
            except Exception:
                pass

    def _seek_release(self) -> None:
        if self._mpv is not None and self._duration_s > 0:
            try:
                frac = min(self.seek_slider.value() / 1000.0, 0.90)
                target = frac * self._duration_s
                self._mpv.seek(target, "absolute")
                self._mpv["pause"] = False
                self._paused = False
                self.btn_play.setText(_G_PAUSE)
            except Exception as e:
                logger.warning("seek failed: %s", e)
        self._dragging = False

    def set_paused_ui(self, paused: bool) -> None:
        """Sync the play/pause button glyph to an externally-set pause state
        (global pause toggle, web remote) using the VS15 monochrome glyphs."""
        self.btn_play.setText(_G_PLAY if paused else _G_PAUSE)

    def _toggle_play(self) -> None:
        if self._mpv is None:
            return
        try:
            new_pause = not bool(self._mpv["pause"])
            self._mpv["pause"] = new_pause
            self._paused = new_pause
            self.btn_play.setText(_G_PLAY if new_pause else _G_PAUSE)
        except Exception as e:
            logger.debug("toggle_play failed: %s", e)

    def _toggle_loop(self) -> None:
        self.looping = self.btn_loop.isChecked()
        if self._mpv is not None:
            try:
                self._mpv["loop-file"] = "inf" if self.looping else "no"
            except Exception as e:
                logger.debug("toggle_loop failed: %s", e)

    def _enable_audio_track(self) -> None:
        """Lazily (re)select an audio track the first time a cell is unmuted.

        Muted cells load with aid=no (see _ensure_mpv) so the grid doesn't
        decode audio it never plays; this restores the track on demand without
        a full reload. No-op once a track is already selected.

        Selecting a track mid-stream starts its decoder cold with an empty
        buffer, and because video_sync=audio the master clock immediately
        follows this half-filled track — playback stutters until audio_buffer
        fills. Re-seeking to the current position flushes and refills both
        decoders from a common point so the clock re-locks cleanly (one brief
        blip on unmute instead of sustained choppiness).
        """
        if self._audio_started or self._mpv is None:
            return
        try:
            self._mpv["aid"] = "auto"
            self._audio_started = True
            pos = self._mpv["time-pos"]
            if pos is not None:
                self._mpv.seek(pos, "absolute")
        except Exception as e:
            logger.debug("enable audio track failed: %s", e)

    def _toggle_mute(self) -> None:
        muted = self.btn_mute.isChecked()
        self.muted = muted
        if not muted:
            self._enable_audio_track()
        if self._mpv is not None:
            try:
                self._mpv["mute"] = muted
            except Exception as e:
                logger.debug("toggle_mute failed: %s", e)
        self.btn_mute.setText(_G_MUTE if muted else _G_UNMUTE)
        if not muted and self.vol_slider.value() == 0:
            self.vol_slider.setValue(70)

    def _vol_changed(self, val: int) -> None:
        if self._mpv is not None:
            try:
                self._mpv["volume"] = float(val)
            except Exception as e:
                logger.debug("vol_changed failed: %s", e)
        if val > 0 and self.muted:
            self.muted = False
            self._enable_audio_track()
            if self._mpv is not None:
                try:
                    self._mpv["mute"] = False
                except Exception as e:
                    logger.debug("vol_changed mute-clear failed: %s", e)
            self.btn_mute.setChecked(False)
            self.btn_mute.setText(_G_UNMUTE)
        elif val == 0 and not self.muted:
            self.muted = True
            if self._mpv is not None:
                try:
                    self._mpv["mute"] = True
                except Exception as e:
                    logger.debug("vol_changed mute-set failed: %s", e)
            self.btn_mute.setChecked(True)
            self.btn_mute.setText(_G_MUTE)

    def _toggle_tag(self) -> None:
        if not self.current_item:
            return
        raw = self.current_item.setdefault("Tags", [])
        tags = (
            [t.get("Name", "") for t in raw]
            if raw and isinstance(raw[0], dict)
            else list(raw)
        )
        if "ToDelete" in tags:
            tags.remove("ToDelete")
        else:
            tags.append("ToDelete")
        self.current_item["Tags"] = tags
        self.btn_tag.setChecked("ToDelete" in tags)  # :checked tints the glyph red
        self.controller.update_tags(self.current_item)

    def _toggle_fav(self) -> None:
        if not self.current_item:
            return
        new = self.btn_fav.isChecked()  # :checked tints the glyph gold
        self.current_item.setdefault("UserData", {})["IsFavorite"] = new
        self.controller.update_favorite(self.current_item["Id"], new)

    # ── EOF / error handling ──────────────────────────────────────────────

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
        if self._switching or self._track_done:
            return
        try:
            if self._mpv["eof-reached"] is not True:
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
            self._show_title_overlay("Media unavailable — retrying soon…")
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
