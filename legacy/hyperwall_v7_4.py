"""
PROJECT: HYPERWALL
VERSION: 7.4
AUTHOR: Thomas Connally
DATE: May 2026

Layout uses QVBoxLayout [QVideoWidget (stretch 1)] + [controls QFrame].
Absolute-geometry overlay (7.0/7.1) caused Qt's ffmpeg/D3D backend to create a
native Win32 child HWND that swallowed all keyboard input.  VBoxLayout avoids
this; QShortcut works normally.
"""

import os
import sys
import random
import uuid
import configparser
import logging
import threading
import ctypes
from collections import deque
from logging.handlers import RotatingFileHandler

# ── PyQt6 ──────────────────────────────────────────────────────────────────────
try:
    from PyQt6.QtCore import (
        Qt, QUrl, QEvent, pyqtSignal, QTimer, QThread, QObject, pyqtSlot,
        QPropertyAnimation, QEasingCurve,
    )
    from PyQt6.QtWidgets import (
        QApplication, QWidget, QGridLayout, QHBoxLayout, QVBoxLayout,
        QPushButton, QSlider, QLabel, QMessageBox, QDialog,
        QListWidget, QListWidgetItem, QSpinBox, QMainWindow, QFrame, QGroupBox,
        QGraphicsOpacityEffect,
    )
    from PyQt6.QtGui import QShortcut, QKeySequence
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PyQt6.QtMultimediaWidgets import QVideoWidget
except ImportError as e:
    print(f"FATAL: PyQt6 not installed — {e}")
    sys.exit(1)

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("FATAL: requests not installed")
    sys.exit(1)

# ── Logging ────────────────────────────────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hyperwall.log")
logger = logging.getLogger("HyperWall")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
_fh = RotatingFileHandler(_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_ch)


def _handle_exception(et, ev, tb):
    if issubclass(et, KeyboardInterrupt):
        sys.__excepthook__(et, ev, tb)
        return
    logger.critical("UNHANDLED EXCEPTION", exc_info=(et, ev, tb))


sys.excepthook = _handle_exception

# ── Kernel tuning ──────────────────────────────────────────────────────────────
os.environ.update({
    "QT_MEDIA_BACKEND":                   "ffmpeg",
    "QT_FFMPEG_DECODING_HW_DEVICE_TYPES": "d3d11va,cuda,dxva2",
    "QT_FFMPEG_ACQUIRE_BUFFER_SIZE":      "268435456",
    "QT_LOGGING_RULES":                   "*.debug=false;qt.multimedia*=false;qt.core*=false",
})
try:
    ctypes.windll.kernel32.SetPriorityClass(
        ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080
    )
    logger.info("Kernel: Priority set to HIGH.")
except Exception:
    pass

# ── Stylesheet ─────────────────────────────────────────────────────────────────
CTRL_STYLE = """
    QFrame#controls {
        background: rgba(55, 55, 55, 220);
        border-top: 1px solid rgba(255, 255, 255, 18);
    }
    QLabel {
        color: #ccc; font-family: 'Segoe UI'; font-size: 9px;
        background: transparent;
    }
    QPushButton {
        background: rgba(80, 80, 80, 180); border: 1px solid rgba(255,255,255,20);
        border-radius: 2px; color: #eee; font-size: 11px; padding: 1px;
        min-width: 22px; min-height: 22px; max-width: 22px; max-height: 22px;
    }
    QPushButton:hover   { background: #2563a8; border-color: #3b8edb; color: white; }
    QPushButton:checked { background: #1e4f78; border-color: #3b8edb; color: white; }
    QSlider::groove:horizontal {
        background: rgba(100, 100, 100, 180); height: 3px; border-radius: 1px;
    }
    QSlider::sub-page:horizontal { background: rgba(59, 142, 219, 200); border-radius: 1px; }
    QSlider::handle:horizontal {
        background: rgba(220, 220, 220, 220); width: 8px; margin: -2px 0; border-radius: 4px;
    }
"""


# ==============================================================================
# 1. API SESSION
# ==============================================================================
class EmbyAPISession:
    """Emby HTTP session. Lock only guards auth-state mutations; requests.Session
    is thread-safe for concurrent reads, so get/post/delete run lock-free."""

    def __init__(self, server_url: str, username: str, password: str):
        self.server_url   = server_url.rstrip("/")
        self.username     = username
        self._password    = password
        self.access_token: str | None = None
        self.user_id:      str | None = None
        self._auth_lock   = threading.Lock()
        self._device_id   = f"hyperwall-{os.urandom(4).hex()}"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":      "HyperWall/7.4",
            "Accept":          "application/json",
            "Accept-Encoding": "gzip, deflate",
        })

    def test_connection(self) -> bool:
        try:
            r = self.session.get(
                f"{self.server_url}/System/Info/Public", timeout=5, verify=False
            )
            return r.status_code == 200
        except Exception:
            return False

    def authenticate(self) -> bool:
        with self._auth_lock:
            try:
                r = self.session.post(
                    f"{self.server_url}/Users/AuthenticateByName",
                    headers={
                        "Content-Type": "application/json",
                        "X-Emby-Authorization": (
                            f'MediaBrowser Client="HyperWall", Device="PC", '
                            f'DeviceId="{self._device_id}", Version="7.4"'
                        ),
                    },
                    json={"Username": self.username, "Pw": self._password},
                    timeout=10, verify=False,
                )
                r.raise_for_status()
                data              = r.json()
                self.access_token = data.get("AccessToken")
                self.user_id      = data.get("User", {}).get("Id")
                logger.info(f"Authenticated. User ID: {self.user_id}")
                return bool(self.access_token and self.user_id)
            except Exception as e:
                logger.error(f"Authentication error: {e}")
                return False

    def _headers(self) -> dict:
        return {"X-Emby-Token": self.access_token}

    def get(self, path: str, **kw) -> requests.Response:
        return self.session.get(
            f"{self.server_url}{path}", headers=self._headers(), verify=False, **kw
        )

    def post(self, path: str, **kw) -> requests.Response:
        return self.session.post(
            f"{self.server_url}{path}", headers=self._headers(), verify=False, **kw
        )

    def delete(self, path: str, **kw) -> requests.Response:
        return self.session.delete(
            f"{self.server_url}{path}", headers=self._headers(), verify=False, **kw
        )

    def close(self):
        self.session.close()


# ==============================================================================
# 2. BACKGROUND WORKERS
# ==============================================================================
class CleanupWorker(QObject):
    finished = pyqtSignal(int, int)
    progress = pyqtSignal(str)

    def __init__(self, api: EmbyAPISession):
        super().__init__()
        self.api        = api
        self._cancelled = False

    @pyqtSlot()
    def run(self):
        logger.info("Maintenance: Starting cleanup...")
        try:
            r = self.api.get(
                f"/Users/{self.api.user_id}/Items",
                params={
                    "Recursive":          "true",
                    "IncludeItemTypes":   "Video,MusicVideo,Movie,Episode",
                    "Tags":               "ToDelete",
                    "Limit":              "500",
                },
                timeout=10,
            )
            items = r.json().get("Items", [])
            if not items:
                self.finished.emit(0, 0)
                return

            ok, fail = 0, 0
            for item in items:
                if self._cancelled:
                    break
                name = item.get("Name", "Unknown")
                self.progress.emit(name)
                try:
                    self.api.delete(f"/Items/{item['Id']}", timeout=7)
                    logger.info(f"Maintenance: Deleted '{name}'")
                    ok += 1
                except Exception as e:
                    logger.error(f"Maintenance: Failed '{name}': {e}")
                    fail += 1

            self.finished.emit(ok, fail)
        except Exception as e:
            logger.error(f"Maintenance thread crash: {e}")
            self.finished.emit(0, -1)


class ContentLoaderThread(QThread):
    finished = pyqtSignal(list)
    progress = pyqtSignal(str)

    def __init__(self, api: EmbyAPISession, library_names: list[str]):
        super().__init__()
        self.api           = api
        self.library_names = library_names

    def run(self):
        all_items: list[dict] = []
        try:
            views    = self.api.get(f"/Users/{self.api.user_id}/Views", timeout=10).json().get("Items", [])
            view_map = {v["Name"]: v["Id"] for v in views}

            for lib in self.library_names:
                lid = view_map.get(lib)
                if not lid:
                    logger.warning(f"Library '{lib}' not found on server.")
                    continue
                self.progress.emit(f"Loading '{lib}'…")
                items = self.api.get(
                    f"/Users/{self.api.user_id}/Items",
                    params={
                        "ParentId":         lid,
                        "Recursive":        "true",
                        "IncludeItemTypes": "Video,MusicVideo,Movie,Episode",
                        "Fields":           "MediaSources,MediaStreams,UserData,Tags",
                        "Limit":            "10000",
                    },
                    timeout=30,
                ).json().get("Items", [])
                logger.info(f"Library '{lib}': {len(items)} items")
                all_items.extend(items)

        except Exception as e:
            logger.error(f"Content loader error: {e}")

        self.finished.emit(all_items)


# ==============================================================================
# 3. VIDEO CELL
# ==============================================================================
class _ClickSlider(QSlider):
    """QSlider that jumps to the clicked position instead of paging."""
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            from PyQt6.QtWidgets import QStyle
            self.setSliderPosition(QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(),
                int(event.position().x()), self.width(),
            ))
        super().mousePressEvent(event)


class VideoCell(QWidget):
    """
    Single video player tile.

    Layout (QVBoxLayout — critically NOT absolute geometry):
        [ QVideoWidget          stretch=1 ]
        [ controls QFrame  fixed height   ]   ← hidden by C key

    D3D/ffmpeg backend renders directly to screen, bypassing Qt's painter —
    any widget overlaid on QVideoWidget is invisible. Controls must live in
    the VBox below the video where no D3D surface is present.
    QShortcut works because the controls strip is a keyboard-safe zone.
    """

    request_next = pyqtSignal(object, bool)
    request_prev = pyqtSignal(object)

    MAX_RETRIES     = 3
    CONTROLS_HEIGHT = 36      # px — compact single-strip
    AUTOHIDE_MS     = 5_000   # ms — controls fade out once at startup
    OVERLAY_SHOW_MS = 3_000   # ms — title overlay hold before fade-out

    def __init__(self, controller: "WallController"):
        super().__init__()
        self.controller   = controller
        self.current_item: dict | None = None
        self.history: deque[dict] = deque(maxlen=50)
        self.looping           = False
        self.muted             = True
        self._dragging         = False
        self._retry_count      = 0
        self._force_transcode  = False
        self.controls_visible  = True
        self._last_pos_update  = 0

        self.setStyleSheet("background: black;")

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        # Video surface — fills entire cell; NoFocus prevents D3D HWND focus steal
        self.video_widget = QVideoWidget(self)
        self.video_widget.setStyleSheet("background: black;")
        self.video_widget.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
        self.video_widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        vbox.addWidget(self.video_widget, 1)

        # Controls strip — fixed height; hiding it returns that space to the video
        self._build_controls()
        vbox.addWidget(self.controls_frame)

        # Media player
        self.player = QMediaPlayer(self)
        self.audio  = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_widget)
        self.audio.setMuted(True)

        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)
        self.player.positionChanged.connect(self._on_position)

        # ── Auto-hide: controls fade out once at startup ───────────────────────
        # After this fires, C is the sole toggle — no further auto-hide so the
        # controls don't flash on every video change.
        self._autohide_timer = QTimer(self)
        self._autohide_timer.setSingleShot(True)
        self._autohide_timer.timeout.connect(self._autohide_controls)
        self._autohide_timer.start(self.AUTOHIDE_MS)

        # ── Title overlay ──────────────────────────────────────────────────────
        self._title_overlay = QLabel("", self)
        self._title_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_overlay.setWordWrap(False)
        self._title_overlay.setStyleSheet(
            "color: white;"
            " background: rgba(0,0,0,180);"
            " font-family: 'Segoe UI';"
            " font-size: 13px;"
            " font-weight: 600;"
            " padding: 5px 14px;"
            " border-radius: 4px;"
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

    # ── Controls construction ──────────────────────────────────────────────────

    def _build_controls(self):
        self.controls_frame = QFrame(self)
        self.controls_frame.setObjectName("controls")
        self.controls_frame.setFixedHeight(self.CONTROLS_HEIGHT)
        self.controls_frame.setStyleSheet(CTRL_STYLE)

        # Opacity effect + animation for smooth show/hide
        self._ctrl_effect = QGraphicsOpacityEffect(self.controls_frame)
        self.controls_frame.setGraphicsEffect(self._ctrl_effect)
        self._ctrl_anim = QPropertyAnimation(self._ctrl_effect, b"opacity", self)
        self._ctrl_anim.setDuration(150)
        self._ctrl_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._ctrl_anim.finished.connect(self._on_ctrl_fade_done)

        outer = QVBoxLayout(self.controls_frame)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(1)

        # ── Row 1: seek slider ─────────────────────────────────────────────────
        self.seek_slider = _ClickSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setFixedHeight(10)
        self.seek_slider.sliderPressed.connect(self._seek_press)
        self.seek_slider.sliderReleased.connect(self._seek_release)
        outer.addWidget(self.seek_slider)

        # ── Row 2: buttons + time + title ──────────────────────────────────────
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
        self.btn_tag  = _btn("🗑", checkable=True)
        self.btn_fav  = _btn("⭐", checkable=True)
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

        for w in (self.btn_prev, self.btn_play, self.btn_next,
                  self.btn_loop, self.btn_tag, self.btn_fav, self.btn_mute):
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
        self.btn_loop.clicked.connect(lambda: setattr(self, "looping", self.btn_loop.isChecked()))
        self.btn_tag.clicked.connect(self._toggle_tag)
        self.btn_fav.clicked.connect(self._toggle_fav)
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.vol_slider.valueChanged.connect(self._vol_changed)

    # ── Fade helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_time(ms: int) -> str:
        """Format milliseconds as M:SS or H:MM:SS."""
        s = max(0, ms) // 1000
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _fade_controls(self, visible: bool):
        self._ctrl_anim.stop()
        if visible:
            self.controls_frame.setVisible(True)
        self._ctrl_anim.setStartValue(self._ctrl_effect.opacity())
        self._ctrl_anim.setEndValue(0.65 if visible else 0.0)
        self._ctrl_anim.start()

    def _on_ctrl_fade_done(self):
        if self._ctrl_effect.opacity() < 0.01:
            self.controls_frame.setVisible(False)

    def _autohide_controls(self):
        """One-shot startup auto-hide; syncs WallController state."""
        self.controls_visible = False
        self.controller.controls_visible = False
        self._fade_controls(False)

    # ── Overlay helpers ────────────────────────────────────────────────────────

    def _show_title_overlay(self, title: str):
        self._overlay_show_timer.stop()
        self._overlay_anim.stop()
        self._title_overlay.setText(title)
        self._overlay_effect.setOpacity(1.0)
        self._title_overlay.adjustSize()
        self._reposition_overlay()
        self._title_overlay.show()
        self._title_overlay.raise_()
        self._overlay_show_timer.start(self.OVERLAY_SHOW_MS)

    def _reposition_overlay(self):
        vw  = self.video_widget
        ovl = self._title_overlay
        ovl.adjustSize()
        w = min(ovl.sizeHint().width(), max(vw.width() - 24, 0))
        h = ovl.sizeHint().height()
        x = vw.x() + (vw.width() - w) // 2
        y = vw.y() + vw.height() - h - 20
        ovl.setFixedWidth(w)
        ovl.move(x, y)

    def _fade_overlay_out(self):
        self._overlay_anim.setStartValue(1.0)
        self._overlay_anim.setEndValue(0.0)
        self._overlay_anim.start()

    def _on_overlay_fade_done(self):
        if self._overlay_effect.opacity() < 0.01:
            self._title_overlay.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._title_overlay.isVisible():
            self._reposition_overlay()

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_controls_visible(self, visible: bool):
        self.controls_visible = visible
        self._autohide_timer.stop()   # cancel startup auto-hide on manual toggle
        self._fade_controls(visible)

    def play(self, item: dict, url: str):
        self.current_item     = item
        self._retry_count     = 0
        self._force_transcode = False
        title = item.get("Name", "Unknown")
        self.lbl_title.setText(title)

        raw_tags  = item.get("Tags", [])
        tag_names = (
            [t.get("Name", "") for t in raw_tags]
            if raw_tags and isinstance(raw_tags[0], dict)
            else raw_tags
        )
        self.btn_tag.setChecked("ToDelete" in tag_names)
        self.btn_fav.setChecked(item.get("UserData", {}).get("IsFavorite", False))

        self.player.setSource(QUrl(url))
        self.player.play()
        self.btn_play.setText("⏸")

        # Show title overlay — visible even when controls are hidden
        self._show_title_overlay(title)

    def release(self):
        self.player.stop()
        self.player.setSource(QUrl())

    # ── Player signals ─────────────────────────────────────────────────────────

    def _on_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self.looping:
                self.player.setPosition(0)
                self.player.play()
            else:
                self.request_next.emit(self, False)
        elif status == QMediaPlayer.MediaStatus.BufferedMedia:
            self._retry_count = 0

    def _on_error(self):
        self._retry_count += 1
        err = self.player.errorString()
        if "Could not open file" not in err:
            logger.warning(
                f"Playback error (attempt {self._retry_count}/{self.MAX_RETRIES}): {err}"
            )
        if self._retry_count <= self.MAX_RETRIES:
            if self._retry_count >= 2 and not self._force_transcode:
                self._force_transcode = True
                logger.info("Escalating to server transcode after repeated failures.")
            QTimer.singleShot(
                (2 ** self._retry_count) * 1000,
                lambda: self.request_next.emit(self, True),
            )
        else:
            logger.error("Max retries reached — skipping.")
            self._force_transcode = False
            self.request_next.emit(self, False)

    def _on_position(self, pos: int):
        if abs(pos - self._last_pos_update) < 250:
            return
        self._last_pos_update = pos
        # Skip slider/label paint when controls are hidden — saves ~4 widget
        # repaints per second per cell across the wall.
        if not self.controls_visible:
            return
        dur = self.player.duration()
        if not self._dragging and dur > 0:
            self.seek_slider.setValue(int(pos / dur * 1000))
        self.lbl_time.setText(f"{self._fmt_time(pos)} / {self._fmt_time(dur)}")

    # ── Controls ───────────────────────────────────────────────────────────────

    def _seek_press(self):
        self._dragging = True
        self._autohide_timer.stop()   # don't auto-hide mid-seek
        self.player.pause()

    def _seek_release(self):
        val = self.seek_slider.value()
        if self.player.duration() > 0:
            self.player.setPosition(int(val / 1000 * self.player.duration()))
        self.player.play()
        self.btn_play.setText("⏸")
        self._dragging = False

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.btn_play.setText("▶")
        else:
            self.player.play()
            self.btn_play.setText("⏸")

    def _toggle_mute(self):
        muted = self.btn_mute.isChecked()
        self.muted = muted
        self.audio.setMuted(muted)
        self.btn_mute.setText("🔇" if muted else "🔊")
        if not muted and self.vol_slider.value() == 0:
            self.vol_slider.setValue(70)

    def _vol_changed(self, val: int):
        self.audio.setVolume(val / 100.0)
        if val > 0 and self.audio.isMuted():
            self.audio.setMuted(False)
            self.muted = False
            self.btn_mute.setChecked(False)
            self.btn_mute.setText("🔊")
        elif val == 0 and not self.audio.isMuted():
            self.audio.setMuted(True)
            self.muted = True
            self.btn_mute.setChecked(True)
            self.btn_mute.setText("🔇")

    def _toggle_tag(self):
        if not self.current_item:
            return
        raw = self.current_item.setdefault("Tags", [])
        if raw and isinstance(raw[0], dict):
            tag_strings = [t.get("Name", "") for t in raw]
        else:
            tag_strings = list(raw)

        if "ToDelete" in tag_strings:
            tag_strings.remove("ToDelete")
        else:
            tag_strings.append("ToDelete")

        self.current_item["Tags"] = tag_strings
        self.btn_tag.setChecked("ToDelete" in tag_strings)
        self.controller.update_tags(self.current_item)

    def _toggle_fav(self):
        if not self.current_item:
            return
        new_state = self.btn_fav.isChecked()
        self.current_item.setdefault("UserData", {})["IsFavorite"] = new_state
        self.controller.update_favorite(self.current_item["Id"], new_state)


# ==============================================================================
# 4. WALL CONTROLLER
# ==============================================================================
class WallController:
    """Manages displays, cells, routing, global shortcuts, and API workers."""

    # ── Routing thresholds ────────────────────────────────────────────────────
    # DIRECT  (≤ 80 Mbps)   — static file serve; full bitrate, zero server load.
    # REMUX   (80–120 Mbps)  — HLS stream-copy; audio→AAC stereo for lossless tracks.
    # TRANSCODE (> 120 Mbps) — QSV H264 re-encode at 80 Mbps / 1080p.
    DIRECT_THRESHOLD       = 80_000_000     # bps — H.264 1080p ceiling for client-side decode
    REMUX_THRESHOLD        = 120_000_000    # bps — above this, server transcodes
    HEVC_TRANSCODE_BITRATE = 40_000_000     # bps — transcode HEVC above this regardless of overall bitrate
    TRANSCODE_BITRATE      = 80_000_000     # target bps for full re-encode
    STREAM_START_STAGGER_MS = 300           # ms between cell starts — lets each HW decoder init before the next

    # Audio codecs that cannot pass through Qt6/ffmpeg cleanly at real-time
    # speeds — lossless multi-channel formats that are CPU-intensive to decode.
    _BAD_AUDIO = frozenset({
        "truehd", "mlp",            # Dolby TrueHD / MLP lossless
        "dtshd_ma", "dts-hd ma",    # DTS-HD Master Audio
        "dtshd_hra",                # DTS-HD High Resolution
    })

    def __init__(self, settings: dict, api: EmbyAPISession):
        self.settings         = settings
        self.api              = api
        self.cells:    list[VideoCell]   = []
        self.windows:  list[QMainWindow] = []
        self.all_items: list[dict]       = []
        self.filtered:  list[dict]       = []
        self.playlist:  deque[dict]      = deque()
        self.controls_visible = True

        self._build_displays()
        self._start_async_load()

    # ── Display setup ──────────────────────────────────────────────────────────

    def _build_displays(self):
        rows, cols = self.settings["grid"]

        for screen in self.settings["screens"]:
            win = QMainWindow()
            win.setWindowTitle(f"HyperWall — {screen.name()}")
            win.setStyleSheet("background: black;")

            cw = QWidget()
            win.setCentralWidget(cw)
            grid = QGridLayout(cw)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(0)

            for r in range(rows):
                for c in range(cols):
                    cell = VideoCell(self)
                    cell.request_next.connect(self.next_video)
                    cell.request_prev.connect(self.prev_video)
                    grid.addWidget(cell, r, c)
                    self.cells.append(cell)

            # QShortcut works here because VideoCell uses QVBoxLayout — the
            # video widget is never the sole surface covering the whole window,
            # so its D3D HWND does not intercept all keyboard messages.
            sc = [
                ("C",      self._global_toggle_controls),
                ("Space",  self._global_toggle_pause),
                ("M",      self._global_toggle_mute),
                ("F",      lambda: self._set_filter("favorites")),
                ("A",      lambda: self._set_filter("all")),
                ("Escape", self._shutdown),
            ]
            for key, fn in sc:
                QShortcut(QKeySequence(key), win).activated.connect(fn)

            win.setGeometry(screen.geometry())
            win.showFullScreen()
            self.windows.append(win)
            logger.info(f"Display active: {screen.name()}")

    # ── Async content load ─────────────────────────────────────────────────────

    def _start_async_load(self):
        self.loader = ContentLoaderThread(self.api, self.settings["libraries"])
        self.loader.finished.connect(self._on_items_loaded)
        self.loader.start()

    def _on_items_loaded(self, items: list[dict]):
        self.all_items = items
        self.filtered  = items[:]
        logger.info(f"Metadata Index: {len(items)} items loaded.")

        if not items:
            logger.warning("No items returned — check config.ini libraries.")
            for cell in self.cells:
                lbl = QLabel("No items found\u2014check config.ini libraries", cell)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setStyleSheet(
                    "color: #666; font-size: 13px; font-family: 'Segoe UI';"
                    " background: transparent;"
                )
                lbl.resize(cell.video_widget.size())
                lbl.show()
            return

        for i, cell in enumerate(self.cells):
            QTimer.singleShot(
                i * self.STREAM_START_STAGGER_MS,
                lambda c=cell: self.next_video(c, False),
            )

    # ── Stream routing ─────────────────────────────────────────────────────────

    def _build_url(self, item: dict, force_transcode: bool = False) -> str:
        sources = item.get("MediaSources", [])
        source  = sources[0] if sources else {}
        bitrate = source.get("Bitrate") or 0
        iid     = item["Id"]
        key     = self.api.access_token
        base    = self.api.server_url
        sid     = uuid.uuid4().hex   # unique play-session for Emby bookkeeping

        # Inspect streams: video for codec/resolution, audio for problem codecs.
        streams      = source.get("MediaStreams") or item.get("MediaStreams") or []
        audio_codecs = {
            (s.get("Codec") or "").lower()
            for s in streams
            if s.get("Type") == "Audio"
        }
        bad_audio = bool(audio_codecs & self._BAD_AUDIO)

        video_stream = next((s for s in streams if s.get("Type") == "Video"), {})
        v_width  = video_stream.get("Width")  or 0
        v_height = video_stream.get("Height") or 0
        v_codec  = (video_stream.get("Codec") or "").lower()
        is_4k    = v_width > 1920 or v_height > 1080
        is_hevc  = v_codec in ("hevc", "h265")

        # ── Tier selection ──────────────────────────────────────────────────────
        # 4K source        → always TRANSCODE (client never sees 4K data)
        # HEVC > 40 Mbps   → TRANSCODE (HEVC decode is much heavier than H.264)
        # > 120 Mbps       → TRANSCODE
        # > 80 Mbps or bad audio → REMUX (server muxes only)
        # else             → DIRECT (client decodes static file at full bitrate)
        if force_transcode or is_4k or bitrate > self.REMUX_THRESHOLD:
            tier = "transcode"
        elif is_hevc and bitrate > self.HEVC_TRANSCODE_BITRATE:
            tier = "transcode"
        elif bitrate > self.DIRECT_THRESHOLD or bad_audio:
            tier = "remux"
        else:
            tier = "direct"

        v_info = f"{v_codec or '?'} {v_width}x{v_height}" if v_width else (v_codec or "?")

        if tier == "direct":
            logger.info(
                f"[DIRECT]    {bitrate/1e6:.1f} Mbps  {v_info}  {item.get('Name')}"
            )
            return (
                f"{base}/Videos/{iid}/stream"
                f"?api_key={key}&static=true"
            )

        if tier == "remux":
            logger.info(
                f"[REMUX]     {bitrate/1e6:.1f} Mbps  {v_info}  bad_audio={bad_audio}  {item.get('Name')}"
            )
            return (
                f"{base}/Videos/{iid}/master.m3u8"
                f"?api_key={key}"
                f"&VideoCodec=copy"
                f"&AudioCodec=aac"
                f"&MaxAudioChannels=2"
                f"&PlaySessionId={sid}"
            )

        # tier == "transcode"
        logger.info(
            f"[TRANSCODE] {bitrate/1e6:.1f} Mbps  {v_info}  4k={is_4k} hevc={is_hevc} force={force_transcode}  {item.get('Name')}"
        )
        return (
            f"{base}/Videos/{iid}/master.m3u8"
            f"?api_key={key}"
            f"&VideoCodec=h264"
            f"&AudioCodec=aac"
            f"&MaxAudioChannels=2"
            f"&MaxHeight=1080"
            f"&MaxWidth=1920"
            f"&VideoBitrate={self.TRANSCODE_BITRATE}"
            f"&PlaySessionId={sid}"
        )

    def next_video(self, cell: VideoCell, is_retry: bool = False):
        if not self.filtered:
            return
        if is_retry and cell.current_item:
            # Pass the cell's escalation flag so a failing stream gets a
            # different (lower-bitrate) URL on retry rather than the same one.
            cell.play(
                cell.current_item,
                self._build_url(cell.current_item, cell._force_transcode),
            )
            return
        if cell.current_item:
            cell.history.append(cell.current_item)
        if not self.playlist:
            shuffled = self.filtered[:]
            random.shuffle(shuffled)
            self.playlist = deque(shuffled)
        item = self.playlist.pop()
        cell.play(item, self._build_url(item))

    def prev_video(self, cell: VideoCell):
        if cell.history:
            item = cell.history.pop()
            cell.play(item, self._build_url(item))

    # ── Global shortcuts ───────────────────────────────────────────────────────

    def _global_toggle_controls(self):
        self.controls_visible = not self.controls_visible
        for cell in self.cells:
            cell.set_controls_visible(self.controls_visible)
        logger.info(f"Controls: {'VISIBLE' if self.controls_visible else 'HIDDEN'}")

    def _global_toggle_pause(self):
        any_playing = any(
            c.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            for c in self.cells
        )
        for cell in self.cells:
            if any_playing:
                if cell.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                    cell.player.pause()
                    cell.btn_play.setText("▶")
            else:
                if cell.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
                    cell.player.play()
                    cell.btn_play.setText("⏸")

    def _global_toggle_mute(self):
        mute_all = any(not c.muted for c in self.cells)
        for cell in self.cells:
            cell.muted = mute_all
            cell.audio.setMuted(mute_all)
            cell.btn_mute.setChecked(mute_all)
            cell.btn_mute.setText("🔇" if mute_all else "🔊")

    def _set_filter(self, mode: str):
        if mode == "favorites":
            subset = [i for i in self.all_items if i.get("UserData", {}).get("IsFavorite")]
            if not subset:
                logger.warning("Filter: No favorites found.")
                return
            self.filtered = subset
        else:
            self.filtered = self.all_items[:]
        self.playlist.clear()
        logger.info(f"Filter: {mode.upper()} ({len(self.filtered)} items)")
        for cell in self.cells:
            self.next_video(cell, False)

    # ── API workers ────────────────────────────────────────────────────────────

    def update_tags(self, item: dict):
        iid  = item["Id"]
        name = item.get("Name", "Unknown")
        raw  = item.get("Tags", [])
        tags = (
            [t.get("Name", "") for t in raw]
            if raw and isinstance(raw[0], dict)
            else list(raw)
        )

        def _worker():
            try:
                data = self.api.get(f"/Users/{self.api.user_id}/Items/{iid}", timeout=7).json()
                data["Tags"] = tags
                for k in [
                    "ServerId", "Etag", "DateCreated", "CanDelete", "CanDownload",
                    "UserData", "Chapters", "ImageTags", "BackdropImageTags",
                    "TagItems", "ExternalUrls", "PlayAccess",
                ]:
                    data.pop(k, None)
                self.api.post(f"/Items/{iid}", json=data, timeout=7)
                logger.info(f"API: Tags updated for '{name}'")
            except Exception as e:
                logger.error(f"API: Tag error for '{name}': {e}")
        threading.Thread(target=_worker, daemon=True).start()

    def update_favorite(self, item_id: str, state: bool):
        def _worker():
            try:
                path = f"/Users/{self.api.user_id}/FavoriteItems/{item_id}"
                (self.api.post if state else self.api.delete)(path, timeout=7)
                logger.info(f"API: Favorite toggled for {item_id} → {state}")
            except Exception as e:
                logger.error(f"API: Favorite error: {e}")
        threading.Thread(target=_worker, daemon=True).start()

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def _shutdown(self):
        logger.info("Shutdown requested.")
        QApplication.instance().quit()

    def _cleanup(self):
        for cell in self.cells:
            try:
                cell.release()
            except Exception:
                pass
        self.api.close()
        logger.info("Cleanup complete.")


# ==============================================================================
# 5. MOUSE IDLE HIDER
# ==============================================================================
class MouseIdleHider(QObject):
    """Hides the cursor after IDLE_MS of no mouse movement; restores on move."""
    IDLE_MS = 3_000

    def __init__(self):
        super().__init__()
        self._hidden = False
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.IDLE_MS)
        self._timer.timeout.connect(self._hide)
        QApplication.instance().installEventFilter(self)
        self._timer.start()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseMove:
            if self._hidden:
                QApplication.restoreOverrideCursor()
                self._hidden = False
            self._timer.start()
        return False

    def _hide(self):
        if not self._hidden:
            QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)
            self._hidden = True


# ==============================================================================
# 6. SETUP WIZARD
# ==============================================================================
class SetupWizard(QDialog):
    def __init__(self, config: configparser.ConfigParser, screens, libraries: list[str]):
        super().__init__()
        self.setWindowTitle("HyperWall 7.4")
        self.resize(720, 540)
        self.setStyleSheet("""
            QDialog { background: #0e0e0e; color: #eee; font-family: 'Segoe UI'; }
            QGroupBox {
                border: 1px solid #2a2a2a; border-radius: 4px; margin-top: 8px;
                font-weight: bold; font-size: 11px; color: #3b8edb; background: #141414;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
            QListWidget {
                background: #181818; border: 1px solid #2a2a2a; color: #ccc; outline: none;
            }
            QListWidget::item:selected { background: #1e4f78; color: white; }
            QSpinBox { background: #181818; color: white; border: 1px solid #333; padding: 4px; min-width: 50px; }
            QPushButton {
                background: #1e4f78; color: white; border: none; padding: 10px 24px;
                font-weight: bold; border-radius: 4px; font-size: 13px;
                min-width: 0; min-height: 0; max-width: 9999px; max-height: 9999px;
            }
            QPushButton:hover { background: #3b8edb; }
            QLabel { color: #888; font-size: 11px; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        title = QLabel("HYPERWALL  7.4")
        title.setStyleSheet("font-size: 24px; font-weight: 900; color: white; letter-spacing: 3px;")
        layout.addWidget(title)

        panels = QHBoxLayout()
        panels.setSpacing(14)

        grp_disp = QGroupBox("DISPLAYS")
        l_disp   = QVBoxLayout(grp_disp)
        self.list_disp = QListWidget()
        self.list_disp.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self._screen_map: dict[str, object] = {}
        last_screens = config.get("Settings", "last_screens", fallback="").split(",")
        for s in screens:
            label = f"{s.name()}  [{s.geometry().width()}×{s.geometry().height()}]"
            item  = QListWidgetItem(label)
            self.list_disp.addItem(item)
            self._screen_map[label] = s
            if s.name() in last_screens:
                item.setSelected(True)
        l_disp.addWidget(self.list_disp)
        panels.addWidget(grp_disp)

        grp_lib = QGroupBox("SOURCES")
        l_lib   = QVBoxLayout(grp_lib)
        self.list_lib = QListWidget()
        self.list_lib.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        last_libs = config.get("Settings", "last_libraries", fallback="").split(",")
        for lib in libraries:
            item = QListWidgetItem(lib)
            self.list_lib.addItem(item)
            if lib in last_libs:
                item.setSelected(True)
        l_lib.addWidget(self.list_lib)
        panels.addWidget(grp_lib)

        layout.addLayout(panels)

        grp_grid = QGroupBox("LAYOUT")
        l_grid   = QHBoxLayout(grp_grid)
        self.rows = QSpinBox()
        self.rows.setRange(1, 6)
        self.rows.setValue(int(config.get("Settings", "last_grid_rows", fallback="2")))
        self.cols = QSpinBox()
        self.cols.setRange(1, 6)
        self.cols.setValue(int(config.get("Settings", "last_grid_cols", fallback="2")))
        l_grid.addWidget(QLabel("ROWS"))
        l_grid.addWidget(self.rows)
        l_grid.addSpacing(20)
        l_grid.addWidget(QLabel("COLS"))
        l_grid.addWidget(self.cols)
        l_grid.addStretch()
        btn = QPushButton("▶   INITIALIZE SYSTEM")
        btn.clicked.connect(self.accept)
        l_grid.addWidget(btn)
        layout.addWidget(grp_grid)

    def get_settings(self) -> dict:
        return {
            "screens":   [self._screen_map[i.text()] for i in self.list_disp.selectedItems()],
            "libraries": [i.text() for i in self.list_lib.selectedItems()],
            "grid":      (self.rows.value(), self.cols.value()),
        }


# ==============================================================================
# 7. ENTRY POINT
# ==============================================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    _mouse_hider = MouseIdleHider()

    CONFIG_FILE = "config.ini"
    cfg = configparser.ConfigParser()

    if not os.path.exists(CONFIG_FILE):
        cfg["Login"] = {
            "server_url": "http://localhost:8096",
            "username":   "",
            "password":   "",
        }
        cfg["Settings"] = {
            "last_screens":       "",
            "last_libraries":     "",
            "last_grid_rows":     "2",
            "last_grid_cols":     "2",
            "cleanup_on_startup": "false",
        }
        with open(CONFIG_FILE, "w") as f:
            cfg.write(f)
        QMessageBox.information(
            None, "Config Created",
            f"config.ini created at:\n{os.path.abspath(CONFIG_FILE)}\n\n"
            "Fill in your Emby server URL, username, and password, then restart.",
        )
        sys.exit(0)

    cfg.read(CONFIG_FILE)

    s_url  = cfg.get("Login", "server_url", fallback="")
    s_user = cfg.get("Login", "username",   fallback="")
    s_pass = cfg.get("Login", "password",   fallback="")

    if not s_url or not s_user:
        QMessageBox.critical(None, "Config Error",
                             "server_url and username must be set in config.ini.")
        sys.exit(1)

    api = EmbyAPISession(s_url, s_user, s_pass)

    if not api.test_connection():
        QMessageBox.critical(None, "Connection Error",
                             f"Cannot reach Emby server at:\n{s_url}")
        sys.exit(1)

    if not api.authenticate():
        QMessageBox.critical(None, "Auth Error",
                             "Authentication failed.\nCheck username and password.")
        sys.exit(1)

    if cfg.getboolean("Settings", "cleanup_on_startup", fallback=False):
        dlg = QDialog()
        dlg.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        dlg.setStyleSheet("background: #111; border: 1px solid #2a2a2a;")
        dlg.setMinimumWidth(340)
        dl  = QVBoxLayout(dlg)
        dl.setContentsMargins(28, 22, 28, 22)
        lbl = QLabel("SYSTEM MAINTENANCE\nPurging tagged items…")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(
            "color: #3b8edb; font-weight: bold; font-size: 13px;"
            " font-family: 'Segoe UI'; background: transparent;"
        )
        dl.addWidget(lbl)
        t = QThread()
        w = CleanupWorker(api)
        w.moveToThread(t)
        w.progress.connect(lambda name: lbl.setText(f"PURGING:\n{name[:42]}"))
        w.finished.connect(lambda ok, fail: (
            logger.info(f"Maintenance: {ok} deleted, {fail} failed."),
            t.quit(),
            dlg.accept(),
        ))
        t.started.connect(w.run)
        t.start()
        dlg.exec()
        t.wait()

    try:
        r    = api.get(f"/Users/{api.user_id}/Views", timeout=10)
        libs = sorted(v["Name"] for v in r.json().get("Items", []))
    except Exception:
        libs = []

    wiz = SetupWizard(cfg, app.screens(), libs)
    if wiz.exec() != QDialog.DialogCode.Accepted:
        api.close()
        sys.exit(0)

    s = wiz.get_settings()
    if not s["screens"] or not s["libraries"]:
        QMessageBox.warning(None, "Setup Error",
                            "Select at least one display and one library.")
        api.close()
        sys.exit(1)

    if not cfg.has_section("Settings"):
        cfg.add_section("Settings")
    cfg.set("Settings", "last_screens",   ",".join(x.name() for x in s["screens"]))
    cfg.set("Settings", "last_libraries", ",".join(s["libraries"]))
    cfg.set("Settings", "last_grid_rows", str(s["grid"][0]))
    cfg.set("Settings", "last_grid_cols", str(s["grid"][1]))
    with open(CONFIG_FILE, "w") as f:
        cfg.write(f)

    logger.info("Initializing HyperWall 7.4…")
    wall = WallController(s, api)
    app.aboutToQuit.connect(wall._cleanup)
    sys.exit(app.exec())
