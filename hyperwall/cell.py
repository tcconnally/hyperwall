import logging
import os
import sys
import platform
import time as _time
from collections import deque
from PyQt6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QEasingCurve, pyqtSignal, pyqtSlot
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFrame, QLabel, QGraphicsOpacityEffect, 
    QPushButton, QSlider, QHBoxLayout, QStyle
)

from .perf import (
    logger, STREAM_START_STAGGER_MS, MAX_RETRIES, CONTROLS_HEIGHT, 
    CONTROLS_OPACITY, AUTOHIDE_MS, OVERLAY_SHOW_MS, MOUSE_IDLE_MS,
    MPV_OPTS, STATS_ENABLED, STATS_COUNTER_PROPS, STATS_INFO_PROPS,
    apply_perf_env, _MPV_LOG_NOISE,
    HISTORY_MAXLEN, PLAYED_ANYTHING_THRESHOLD, MIN_NEXT_INTERVAL_S,
    SEEK_SLIDER_MAX, SEEK_FRACTION_MAX
)

# Late import for mpv to avoid module-level load error
mpv = None
def _import_mpv():
    global mpv
    if mpv is None:
        import mpv as _mpv
        mpv = _mpv

CTRL_STYLE = """
    QFrame#controls {
        background: rgba(55, 55, 55, 220);
        border-top: 1px solid rgba(255, 255, 255, 18);
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

class _ClickSlider(QSlider):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderPosition(QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(),
                int(event.position().x()), self.width(),
            ))
        super().mousePressEvent(event)

class VideoCell(QWidget):
    """Represents a single video playback cell in the HyperWall.

    Manages the mpv player instance, UI controls (play, seek, mute, etc.),
    and interactions with the Emby API for media tagging and favoriting.
    Each VideoCell operates independently within its allocated display area.
    """
    request_next = pyqtSignal(object, bool)
    request_prev = pyqtSignal(object)
    _sig_eof   = pyqtSignal(int, str)
    _sig_time  = pyqtSignal(int, float, float)

    def __init__(self, controller):
        """Initializes a VideoCell.

        Args:
            controller: The main WallController instance orchestrating all cells.
        """
        super().__init__()
        _import_mpv()
        self.controller       = controller
        self.current_item: dict | None = None
        self.history: deque[dict] = deque(maxlen=HISTORY_MAXLEN)
        self.looping          = False
        self.muted            = True
        self._dragging        = False
        self._retry_count     = 0
        self._force_transcode = False
        self.controls_visible = True
        self._mpv = None
        self._mpv_gen         = 0
        self._duration_s      = 0.0
        self._stats_current: dict[str, float]   = {}
        self._stats_total:   dict[str, float]   = {}
        self._stats_info:    dict[str, object]  = {}
        self._played_anything = False
        self._last_next_request_ts = 0.0
        self._emby_session_id: str | None = None
        self._emby_item_id:    str | None = None

        self.setStyleSheet("background: black;")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

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
        vbox.addWidget(self.controls_frame)

        self._autohide_timer = QTimer(self)
        self._autohide_timer.setSingleShot(True)
        self._autohide_timer.timeout.connect(self._autohide_controls)
        self._autohide_timer.start(AUTOHIDE_MS)

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

        self._sig_eof.connect(self._handle_eof, Qt.ConnectionType.QueuedConnection)
        self._sig_time.connect(self._handle_time, Qt.ConnectionType.QueuedConnection)

    def _destroy_mpv(self):
        """Terminates the current mpv player instance and cleans up resources."""
        if self._mpv is None:
            return
        if STATS_ENABLED:
            self._flush_stats()
        try:
            self._mpv.terminate()
        except Exception:
            logger.exception("mpv terminate raised")
        self._mpv = None

    def _flush_stats(self):
        """Collects and flushes current mpv playback statistics into total counters."""
        if self._mpv is not None:
            for prop in STATS_COUNTER_PROPS:
                try:
                    v = self._mpv[prop]
                    if v is not None:
                        self._stats_current[prop] = float(v)
                except Exception:
                    logger.exception("Error fetching mpv counter prop: %s", prop)
            for prop in STATS_INFO_PROPS:
                try:
                    v = self._mpv[prop]
                    if v is not None:
                        self._stats_info[prop] = v
                except Exception:
                    logger.exception("Error fetching mpv info prop: %s", prop)
        for k, v in self._stats_current.items():
            self._stats_total[k] = self._stats_total.get(k, 0.0) + v
        self._stats_current.clear()

    def _ensure_mpv(self):
        """Ensures an mpv player instance is initialized. Creates one if it doesn't exist.

        Handles platform-specific `winId()` masking for Windows and temporary
        redirection of C stdio to suppress noisy FFmpeg logs during initialization.
        """
        if self._mpv is not None:
            return
        # Mask to 32-bit to prevent HWND sign-extension on Windows (mpv #10189).
        # Without this, winId() can become negative on systems with long uptimes,
        # causing mpv to create detached windows instead of embedded playback.
        if platform.system() == "Windows":
            wid = int(self.video_frame.winId()) & 0xFFFFFFFF
        else:
            wid = int(self.video_frame.winId())
        if wid == 0:
            logger.warning("video_frame.winId() == 0 — widget not realized yet.")
            return

        # Suppress FFmpeg C-level log output during instance creation.
        # With 8-12 cells, all instances route logs to the first handler
        # (python-mpv issue #126). Redirect C stdio temporarily.
        _devnull = open(os.devnull, "w")
        _std_saved = (sys.stdout, sys.stderr)
        try:
            sys.stdout = sys.stderr = _devnull
            m = mpv.MPV(wid=wid, log_handler=self._mpv_log, **apply_perf_env(MPV_OPTS))
        finally:
            sys.stdout, sys.stderr = _std_saved
            _devnull.close()
        try: m["mute"] = self.muted
        except Exception:
            logger.exception("Error setting mpv mute property")
        if self.looping:
            try: m["loop-file"] = "inf"
            except Exception:
                logger.exception("Error setting mpv loop-file property")

        self._mpv_gen += 1
        gen = self._mpv_gen

        @m.event_callback("end-file")
        def _on_end_file(ev):
            try:
                reason = ev.event.get("reason", "eof")
            except Exception:
                reason = "Error in _on_end_file callback"
                logger.exception("Error in mpv end-file callback")
            self._sig_eof.emit(gen, str(reason))

        @m.property_observer("time-pos")
        def _on_time(_name, value):
            if value is None:
                return
            if gen != self._mpv_gen:
                return
            if value > PLAYED_ANYTHING_THRESHOLD and not self._played_anything:
                self._played_anything = True
            self._sig_time.emit(gen, float(value), float(self._duration_s or 0))

        @m.property_observer("duration")
        def _on_dur(_name, value):
            if gen != self._mpv_gen:
                return
            if value:
                self._duration_s = float(value)

        if STATS_ENABLED:
            for _prop in STATS_COUNTER_PROPS:
                @m.property_observer(_prop)
                def _on_counter(_name, value, _gen=gen, _prop=_prop):
                    if _gen != self._mpv_gen or value is None:
                        return
                    self._stats_current[_prop] = float(value)
            for _prop in STATS_INFO_PROPS:
                @m.property_observer(_prop)
                def _on_info(_name, value, _gen=gen, _prop=_prop):
                    if _gen != self._mpv_gen or value is None:
                        return
                    self._stats_info[_prop] = value

        self._mpv = m

    def _mpv_log(self, level, component, message):
        """Custom log handler for mpv messages.

        Filters out known noisy warnings and routes messages to the HyperWall logger.

        Args:
            level (str): The log level (e.g., 'warn', 'error', 'info').
            component (str): The mpv component originating the message.
            message (str): The log message content.
        """
        text = message.strip()
        if level == "warn" and any(pat in text for pat in _MPV_LOG_NOISE):
            return
        msg = f"mpv[{component}] {text}"
        if level in ("fatal", "error"):
            logger.error(msg)
        elif level == "warn":
            logger.warning(msg)

    def showEvent(self, event):
        """Handles the show event for the QWidget.
        Ensures the video frame's native window ID is ready.
        """
        super().showEvent(event)
        self.video_frame.winId()

    def resizeEvent(self, event):
        """Handles the resize event for the QWidget.
        Repositions the title overlay if visible.

        Args:
            event (QResizeEvent): The resize event.
        """
        super().resizeEvent(event)
        if self._title_overlay.isVisible():
            self._reposition_overlay()

    def _build_controls(self):
        """Constructs the UI elements for the video controls (slider, buttons, labels)."""
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

        self.seek_slider = _ClickSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, SEEK_SLIDER_MAX)
        self.seek_slider.setFixedHeight(10)
        self.seek_slider.sliderPressed.connect(self._seek_press)
        self.seek_slider.sliderReleased.connect(self._seek_release)
        outer.addWidget(self.seek_slider)

        row = QHBoxLayout()
        row.setSpacing(2); row.setContentsMargins(0, 0, 0, 0)

        def _btn(text: str, checkable: bool = False) -> QPushButton:
            b = QPushButton(text); b.setCheckable(checkable); return b

        self.btn_prev = _btn("⏮")
        self.btn_play = _btn("⏸")
        self.btn_next = _btn("⏭")
        self.btn_loop = _btn("🔁", checkable=True)
        self.btn_tag  = _btn("🗑", checkable=True)
        self.btn_fav  = _btn("⭐", checkable=True)
        self.btn_mute = _btn("🔇", checkable=True); self.btn_mute.setChecked(True)

        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100); self.vol_slider.setValue(0)
        self.vol_slider.setFixedWidth(45); self.vol_slider.setFixedHeight(10)

        self.lbl_time = QLabel("0:00 / 0:00")
        self.lbl_time.setFixedWidth(75)
        self.lbl_time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.lbl_title = QLabel("Initializing…")
        self.lbl_title.setStyleSheet(
            "color: white; font-family: 'Segoe UI'; font-size: 12px;"
            " font-weight: 700; background: transparent;"
        )

        for w in (self.btn_prev, self.btn_play, self.btn_next, self.btn_loop,
                  self.btn_tag, self.btn_fav, self.btn_mute):
            row.addWidget(w)
        row.addSpacing(2); row.addWidget(self.vol_slider)
        row.addSpacing(4); row.addWidget(self.lbl_time)
        row.addSpacing(2); row.addWidget(self.lbl_title, stretch=1)
        outer.addLayout(row)

        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_prev.clicked.connect(lambda: self.request_prev.emit(self))
        self.btn_next.clicked.connect(lambda: self.request_next.emit(self, False))
        self.btn_loop.clicked.connect(self._toggle_loop)
        self.btn_tag.clicked.connect(self._toggle_tag)
        self.btn_fav.clicked.connect(self._toggle_fav)
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.vol_slider.valueChanged.connect(self._vol_changed)

    @staticmethod
    def _fmt_time(s: float) -> str:
        """Formats a time in seconds into a human-readable string (e.g., '1:23' or '1:02:23')."""
        s = max(0, int(s))
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _fade_controls(self, visible: bool):
        """Fades the control bar in or out.

        Args:
            visible (bool): True to show controls, False to hide.
        """
        self._ctrl_anim.stop()
        if visible:
            self.controls_frame.setVisible(True)
        self._ctrl_anim.setStartValue(self._ctrl_effect.opacity())
        self._ctrl_anim.setEndValue(CONTROLS_OPACITY if visible else 0.0)
        self._ctrl_anim.start()

    def _on_ctrl_fade_done(self):
        """Callback for when the control fade animation finishes.
        Hides the control frame if it has faded out completely.
        """
        if self._ctrl_effect.opacity() < 0.01:
            self.controls_frame.setVisible(False)

    def _autohide_controls(self):
        """Hides the controls after a period of inactivity."""
        self.controls_visible = False
        self.controller.controls_visible = False
        self._fade_controls(False)

    def set_controls_visible(self, visible: bool):
        """Sets the visibility of the video controls.

        Args:
            visible (bool): True to show controls, False to hide.
        """
        self.controls_visible = visible
        self._autohide_timer.stop()
        self._fade_controls(visible)

    def _show_title_overlay(self, title: str):
        """Displays a title overlay on the video cell.

        Args:
            title (str): The title text to display.
        """
        self._overlay_show_timer.stop()
        self._overlay_anim.stop()
        self._title_overlay.setText(title)
        self._overlay_effect.setOpacity(1.0)
        self._title_overlay.adjustSize()
        self._reposition_overlay()
        self._title_overlay.show()
        self._title_overlay.raise_()
        self._overlay_show_timer.start(OVERLAY_SHOW_MS)

    def _reposition_overlay(self):
        """Repositions the title overlay to be centered at the bottom of the video frame."""
        vw, ovl = self.video_frame, self._title_overlay
        ovl.adjustSize()
        w = min(ovl.sizeHint().width(), max(vw.width() - 24, 0))
        h = ovl.sizeHint().height()
        x = vw.x() + (vw.width() - w) // 2
        y = vw.y() + vw.height() - h - 20
        ovl.setFixedWidth(w); ovl.move(x, y)

    def _fade_overlay_out(self):
        """Starts the animation to fade out the title overlay."""
        self._overlay_anim.setStartValue(1.0)
        self._overlay_anim.setEndValue(0.0)
        self._overlay_anim.start()

    def _on_overlay_fade_done(self):
        """Callback for when the title overlay fade animation finishes.
        Hides the overlay if it has faded out completely.
        """
        if self._overlay_effect.opacity() < 0.01:
            self._title_overlay.hide()

    def play(self, item: dict, url: str):
        """Starts playback of a new media item in the cell.

        Args:
            item (dict): The Emby media item dictionary.
            url (str): The direct URL for the media stream.
        """
        if self.current_item is not item:
            self._retry_count     = 0
            self._force_transcode = False
        self.current_item     = item
        self._duration_s      = 0.0
        self._played_anything = False

        title = item.get("Name", "Unknown")
        self.lbl_title.setText(title)

        raw = item.get("Tags", [])
        tag_names = ([t.get("Name", "") for t in raw]
                     if raw and isinstance(raw[0], dict) else raw)
        self.btn_tag.setChecked("ToDelete" in tag_names)
        self.btn_fav.setChecked(item.get("UserData", {}).get("IsFavorite", False))

        self._destroy_mpv()
        self._ensure_mpv()
        if self._mpv is None:
            logger.error("mpv not initialized — cannot play.")
            return
        try:
            self._mpv["mute"] = self.muted
            self._mpv.command("loadfile", url)
            self.btn_play.setText("⏸")
        except Exception:
            logger.exception("mpv loadfile failed")
            self._sig_eof.emit(self._mpv_gen, "error")
            return
        self._show_title_overlay(title)

    def release(self):
        """Releases the mpv player instance and associated resources."""
        self._destroy_mpv()

    def _handle_eof(self, gen: int, reason: str):
        """Handles the end-of-file (EOF) event from mpv.

        Triggers next video request or error handling based on the reason.

        Args:
            gen (int): The generation ID of the mpv instance.
            reason (str): The reason for EOF (e.g., 'eof', 'error').
        """
        if gen != self._mpv_gen:
            return
        if reason == "error":
            self._on_error()
            return
        if reason == "eof":
            if not self._played_anything:
                logger.warning("EOF before first frame — treating as error.")
                self._on_error()
                return
            if self.looping and self._mpv is not None:
                try:
                    self._mpv.seek(0, "absolute")
                    self._mpv["pause"] = False
                except Exception:
                    logger.exception("Error seeking mpv player in loop")
            else:
                self._request_next_throttled(False)

    def _request_next_throttled(self, is_retry: bool):
        """Requests the next video with a throttling mechanism to prevent rapid re-requests.

        Args:
            is_retry (bool): True if this is a retry attempt, False otherwise.
        """
        now = _time.monotonic()
        if not is_retry and (now - self._last_next_request_ts) < MIN_NEXT_INTERVAL_S:
            logger.warning("next_video throttled (last fire %.2fs ago)",
                           now - self._last_next_request_ts)
            return
        self._last_next_request_ts = now
        self.request_next.emit(self, is_retry)

    def _on_error(self):
        """Handles playback errors, including retry logic and transcoding escalation.
        If max retries are reached, it requests the next video.
        """
        self._retry_count += 1
        logger.warning("Playback error (attempt %d/%d)", self._retry_count, MAX_RETRIES)
        if self._retry_count <= MAX_RETRIES:
            if self._retry_count >= 2 and not self._force_transcode:
                self._force_transcode = True
                logger.info("Escalating to server transcode after repeated failures.")
            QTimer.singleShot((2 ** self._retry_count) * 1000,
                              lambda: self._request_next_throttled(True))
        else:
            logger.error("Max retries reached — skipping.")
            self._force_transcode = False
            self._request_next_throttled(False)

    def _handle_time(self, gen: int, pos: float, dur: float):
        """Updates the seek slider and time label based on mpv's time position.

        Args:
            gen (int): The generation ID of the mpv instance.
            pos (float): Current playback position in seconds.
            dur (float): Total duration of the media in seconds.
        """
        if gen != self._mpv_gen:
            return
        if not self.controls_visible:
            return
        if not self._dragging and dur > 0:
            self.seek_slider.setValue(int(pos / dur * SEEK_SLIDER_MAX))
        self.lbl_time.setText(f"{self._fmt_time(pos)} / {self._fmt_time(dur)}")

    def _seek_press(self):
        """Handles the event when the seek slider is pressed.
        Pauses playback and stops the autohide timer.
        """
        self._dragging = True
        self._autohide_timer.stop()
        if self._mpv is not None:
            try: self._mpv["pause"] = True
            except Exception:
                logger.exception("Error pausing mpv player during seek")

    def _seek_release(self):
        """Handles the event when the seek slider is released.
        Seeks to the new position and resumes playback.
        """
        if self._mpv is not None and self._duration_s > 0:
            try:
                frac = min(self.seek_slider.value() / SEEK_SLIDER_MAX, SEEK_FRACTION_MAX)
                target = frac * self._duration_s
                self._mpv.seek(target, "absolute")
                self._mpv["pause"] = False
                self.btn_play.setText("⏸")
            except Exception:
                logger.exception("seek failed")
        self._dragging = False

    def _toggle_play(self):
        """Toggles the play/pause state of the video and updates the play button icon."""
        if self._mpv is None: return
        try:
            new_pause = not bool(self._mpv["pause"])
            self._mpv["pause"] = new_pause
            self.btn_play.setText("▶" if new_pause else "⏸")
        except Exception:
            logger.exception("Error toggling play/pause in mpv")

    def _toggle_loop(self):
        """Toggles looping for the current video."""
        self.looping = self.btn_loop.isChecked()
        if self._mpv is not None:
            try:
                self._mpv["loop-file"] = "inf" if self.looping else "no"
            except Exception:
                logger.exception("Error toggling loop-file in mpv")

    def _set_muted(self, state: bool):
        """Sets the mute state of the mpv player and updates the UI.

        Args:
            state (bool): True to mute, False to unmute.
        """
        self.muted = state
        if self._mpv is not None:
            try: self._mpv["mute"] = state
            except Exception:
                logger.exception("Error setting mpv mute state")
        self.btn_mute.setChecked(state)
        self.btn_mute.setText("🔇" if state else "🔊")

    def _toggle_mute(self):
        """Toggles the mute state. If unmuting and volume is 0, sets volume to 70."""
        self._set_muted(not self.muted)
        if not self.muted and self.vol_slider.value() == 0:
            self.vol_slider.setValue(70)

    def _vol_changed(self, val: int):
        """Handles volume slider value changes.
        Updates mpv volume and adjusts mute state if volume reaches 0 or goes above 0.

        Args:
            val (int): New volume value (0-100).
        """
        if self._mpv is not None:
            try: self._mpv["volume"] = float(val)
            except Exception:
                logger.exception("Error setting mpv volume")
        if val > 0 and self.muted:
            self._set_muted(False)
        elif val == 0 and not self.muted:
            self._set_muted(True)

    def _toggle_tag(self):
        """Toggles the 'ToDelete' tag on the current media item in Emby.
        Updates the UI and notifies the controller to update tags via API.
        """
        if not self.current_item: return
        raw = self.current_item.setdefault("Tags", [])
        tags = ([t.get("Name", "") for t in raw]
                if raw and isinstance(raw[0], dict) else list(raw))
        if "ToDelete" in tags: tags.remove("ToDelete")
        else:                  tags.append("ToDelete")
        self.current_item["Tags"] = tags
        self.btn_tag.setChecked("ToDelete" in tags)
        self.controller.update_tags(self.current_item)

    def _toggle_fav(self):
        """Toggles the 'IsFavorite' status of the current media item in Emby.
        Updates the UI and notifies the controller to update favorite status via API.
        """
        if not self.current_item: return
        new = self.btn_fav.isChecked()
        self.current_item.setdefault("UserData", {})["IsFavorite"] = new
        self.controller.update_favorite(self.current_item["Id"], new)