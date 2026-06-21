"""
Hyperwall v9 — VideoCell widget.

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
import sys
import time as _time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeoutError
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
    MAX_RETRIES,
    MOUSE_IDLE_MS,
    MPV_LOG_NOISE,
    MPV_OPTS,
    OVERLAY_SHOW_MS,
    STATS_COUNTER_PROPS,
    STATS_ENABLED,
    STATS_INFO_PROPS,
    apply_env_overrides,
)

logger = logging.getLogger("HyperWall")

CTRL_STYLE = """
    QFrame#controls {
        background: rgba(22, 22, 22, 210);
        border-top: 1px solid rgba(255, 255, 255, 22);
        border-radius: 4px 4px 0 0;
    }
    QLabel { color: #ccc; font-family: 'Segoe UI'; font-size: 9px; background: transparent; }
    QPushButton {
        background: rgba(80, 80, 80, 180); border: 1px solid rgba(255,255,255,20);
        border-radius: 2px; color: #eee; font-size: 11px; padding: 1px;
        min-width: 22px; min-height: 22px; max-width: 22px; max-height: 22px;
    }
    QPushButton:hover   { background: #2563a8; border-color: #3b8edb; color: white; }
    QPushButton:checked { background: #1e4f78; border-color: #3b8edb; color: white; }
    QSlider::groove:horizontal { background: rgba(100,100,100,180); height: 3px; border-radius: 1px; }
    QSlider::sub-page:horizontal { background: rgba(59,142,219,200); border-radius: 1px; }
    QSlider::handle:horizontal {
        background: rgba(220,220,220,220); width: 8px; margin: -2px 0; border-radius: 4px;
    }
"""


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
        self._last_next_request_ts = 0.0
        self._mouse_in_cell = False
        self._emby_session_id: str | None = None
        self._emby_item_id: str | None = None
        self._switching = False  # set in play(), consumed in _handle_eof

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
        self._title_overlay.setStyleSheet(
            "color: white; background: rgba(0,0,0,180);"
            " font-family: 'Segoe UI'; font-size: 13px; font-weight: 600;"
            " padding: 5px 14px; border-radius: 4px;"
        )
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

        self.setMouseTracking(True)
        self._sig_eof.connect(self._handle_eof, Qt.ConnectionType.QueuedConnection)

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
            m = _mpv.MPV(
                wid=str(wid),
                log_handler=self._mpv_log,
                **apply_env_overrides(MPV_OPTS),
            )
        finally:
            sys.stdout, sys.stderr = _std_saved
            _devnull.close()

        # Apply initial state
        try:
            m["mute"] = self.muted
        except Exception as e:
            logger.debug("mpv: failed to set initial mute: %s", e)
        if self.looping:
            try:
                m["loop-file"] = "inf"
            except Exception as e:
                logger.debug("mpv: failed to set initial loop-file: %s", e)

        self._mpv_gen += 1
        gen = self._mpv_gen

        @m.event_callback("end-file")
        def _on_end_file(ev: Any) -> None:
            reason = "eof"
            try:
                reason = ev.event.get("reason", "eof")
            except Exception:
                pass
            self._sig_eof.emit(gen, str(reason))

        @m.property_observer("time-pos")
        def _on_time(_name: str, value: float | None) -> None:
            if value is None or gen != self._mpv_gen:
                return
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

    def _destroy_mpv(self) -> None:
        """Terminate mpv with a bounded timeout."""
        if self._mpv is None:
            return
        if STATS_ENABLED:
            self._flush_stats()
        mpv_ref = self._mpv
        self._mpv = None
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(mpv_ref.terminate)
            try:
                fut.result(timeout=1.5)
            except FutTimeoutError:
                logger.warning("mpv terminate timed out — abandoning process.")
            except Exception as e:
                logger.debug("mpv terminate raised: %s", e)

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

        if self._mpv is None:
            logger.error("mpv not initialized — cannot play.")
            return

        # _switching flag suppresses the stale end-file that loadfile fires
        # when replacing a playing track. Without it, that end-file requests
        # next_video unnecessarily, colliding with the cell's new content.
        self._switching = True
        try:
            self._mpv["mute"] = self.muted
            self._mpv.command("loadfile", url)
            self.btn_play.setText("⏸")
        except Exception as e:
            self._switching = False
            logger.error("mpv loadfile failed: %s", e)
            self._sig_eof.emit(self._mpv_gen, "error")
            return

    def release(self) -> None:
        """Clean up and release all resources."""
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
        self.seek_slider.setFixedHeight(10)
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

        self.btn_prev = _btn("⏮")
        self.btn_play = _btn("⏸")
        self.btn_next = _btn("⏭")
        self.btn_loop = _btn("🔁", checkable=True)
        self.btn_tag = _btn("🗑", checkable=True)
        self.btn_fav = _btn("⭐", checkable=True)
        self.btn_mute = _btn("🔇", checkable=True)
        self.btn_mute.setChecked(True)

        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(0)
        self.vol_slider.setFixedWidth(45)
        self.vol_slider.setFixedHeight(10)

        self.lbl_time = QLabel("0:00 / 0:00")
        self.lbl_time.setFixedWidth(75)
        self.lbl_time.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )

        self.lbl_title = QLabel("Initializing…")
        self.lbl_title.setStyleSheet(
            "color: white; font-family: 'Segoe UI'; font-size: 12px;"
            " font-weight: 700; background: transparent;"
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
        self._title_overlay.setText(title)
        self._overlay_effect.setOpacity(1.0)
        self._title_overlay.adjustSize()
        self._reposition_overlay()
        self._title_overlay.show()
        self._title_overlay.raise_()
        self._overlay_show_timer.start(OVERLAY_SHOW_MS)

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
            except Exception:
                pass

    def _seek_release(self) -> None:
        if self._mpv is not None and self._duration_s > 0:
            try:
                frac = min(self.seek_slider.value() / 1000.0, 0.90)
                target = frac * self._duration_s
                self._mpv.seek(target, "absolute")
                self._mpv["pause"] = False
                self.btn_play.setText("⏸")
            except Exception as e:
                logger.warning("seek failed: %s", e)
        self._dragging = False

    def _toggle_play(self) -> None:
        if self._mpv is None:
            return
        try:
            new_pause = not bool(self._mpv["pause"])
            self._mpv["pause"] = new_pause
            self.btn_play.setText("▶" if new_pause else "⏸")
        except Exception as e:
            logger.debug("toggle_play failed: %s", e)

    def _toggle_loop(self) -> None:
        self.looping = self.btn_loop.isChecked()
        if self._mpv is not None:
            try:
                self._mpv["loop-file"] = "inf" if self.looping else "no"
            except Exception as e:
                logger.debug("toggle_loop failed: %s", e)

    def _toggle_mute(self) -> None:
        muted = self.btn_mute.isChecked()
        self.muted = muted
        if self._mpv is not None:
            try:
                self._mpv["mute"] = muted
            except Exception as e:
                logger.debug("toggle_mute failed: %s", e)
        self.btn_mute.setText("🔇" if muted else "🔊")
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
            if self._mpv is not None:
                try:
                    self._mpv["mute"] = False
                except Exception as e:
                    logger.debug("vol_changed mute-clear failed: %s", e)
            self.btn_mute.setChecked(False)
            self.btn_mute.setText("🔊")
        elif val == 0 and not self.muted:
            self.muted = True
            if self._mpv is not None:
                try:
                    self._mpv["mute"] = True
                except Exception as e:
                    logger.debug("vol_changed mute-set failed: %s", e)
            self.btn_mute.setChecked(True)
            self.btn_mute.setText("🔇")

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
        self.btn_tag.setChecked("ToDelete" in tags)
        self.controller.update_tags(self.current_item)

    def _toggle_fav(self) -> None:
        if not self.current_item:
            return
        new = self.btn_fav.isChecked()
        self.current_item.setdefault("UserData", {})["IsFavorite"] = new
        self.controller.update_favorite(self.current_item["Id"], new)

    # ── EOF / error handling ──────────────────────────────────────────────

    def _handle_eof(self, gen: int, reason: str) -> None:
        if gen != self._mpv_gen:
            return
        if reason == "error":
            self._on_error()
            return
        if reason == "eof":
            if self._switching:
                # loadfile just replaced a playing track — this end-file
                # is the old track ending, not the new one. Suppress the
                # stale next_video request.
                self._switching = False
                return
            if not self._played_anything:
                logger.warning("EOF before first frame — treating as error.")
                self._on_error()
                return
            if self.looping and self._mpv is not None:
                try:
                    self._mpv.seek(0, "absolute")
                    self._mpv["pause"] = False
                except Exception as e:
                    logger.warning("Loop seek failed: %s", e)
                    self._request_next_throttled(False)
            else:
                self._request_next_throttled(False)

    def _request_next_throttled(self, is_retry: bool) -> None:
        MIN_INTERVAL = 0.75
        now = _time.monotonic()
        if not is_retry and (now - self._last_next_request_ts) < MIN_INTERVAL:
            logger.warning(
                "next_video throttled (last fire %.2fs ago)",
                now - self._last_next_request_ts,
            )
            return
        self._last_next_request_ts = now
        self.request_next.emit(self, is_retry)

    def _on_error(self) -> None:
        self._retry_count += 1
        logger.warning(
            "Playback error (attempt %d/%d)", self._retry_count, MAX_RETRIES
        )
        if self._retry_count <= MAX_RETRIES:
            if self._retry_count >= 2 and not self._force_transcode:
                self._force_transcode = True
                logger.info("Escalating to server transcode.")
            QTimer.singleShot(
                (2 ** self._retry_count) * 1000,
                lambda: self._request_next_throttled(True),
            )
        else:
            logger.error("Max retries reached — skipping.")
            self._force_transcode = False
            self._request_next_throttled(False)
