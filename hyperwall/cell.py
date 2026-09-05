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
    QThread,
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
    DECODER_FAULT_MAX,
    MAX_RETRIES,
    MOUSE_IDLE_MS,
    STARVATION_FAULT_EVENTS,
    STARVATION_FAULT_TOTAL_S,
    MPV_LOG_NOISE,
    MPV_OPTS,
    OUTAGE_BACKOFF_S,
    OVERLAY_SHOW_MS,
    STALL_TIMEOUT_S,
    STATS_COUNTER_PROPS,
    STATS_ENABLED,
    STATS_INFO_PROPS,
    TRANSPORT_RETRY_MAX,
    WATCHDOG_INTERVAL_MS,
    _s,
    apply_env_overrides,
    native_wid,
)
from .reliability import (
    apply_jitter,
    audio_track_for_mute,
    classify_playback_fault,
    context_for_prefetch_fault,
    context_for_unscoped_fault,
    is_malformed_stream_fault,
    PlaybackToken,
    playback_token_is_current,
    decoder_recovery_plan,
    end_file_reason,
    escalation_plan,
    is_stalled,
    outage_recovery_plan,
    should_park,
    starvation_fault_reached,
    transport_recovery_plan,
)
from . import theme
from .perftrace import traced
from .playback_plan import PlaybackPlan
from .playback_state import (
    CellPlaybackController,
    PlaybackEvent,
    PlaybackIdentity,
)
from .urls import tag_names

logger = logging.getLogger("HyperWall")

NativeContext = tuple[int, int, str | None, str | None, str | None]


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
    request_solo = pyqtSignal(object)
    resource_quarantined = pyqtSignal(object)
    request_remote_solo = pyqtSignal(object)
    _sig_eof = pyqtSignal(object, str)
    _sig_track_done = pyqtSignal(object)
    _sig_buffering = pyqtSignal(object, bool)
    _sig_decoder_fault = pyqtSignal(object, str)
    _sig_transport_fault = pyqtSignal(object, str)
    _sig_prefetch_fault = pyqtSignal(object, str)
    _sig_prefetched_advance = pyqtSignal(int, bool)
    _sig_play_finished = pyqtSignal(int, bool)
    _sig_release_retry = pyqtSignal()

    def __init__(self, controller: Any):
        super().__init__()
        self.controller = controller
        self._playback_controller = CellPlaybackController()
        self._playback_plan: PlaybackPlan | None = None
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
        self._render_context_released = sys.platform != "darwin"
        self._shutdown_render_release_requested = False
        self._shutdown_render_release_deadline = 0.0
        self._render_finalizer_pending = False
        self._destroy_retry_requested = False
        self._destroy_retry_deadline = 0.0
        self._native_finalizer_lock = threading.Lock()
        self._native_finalizer_records: list[dict[str, Any]] = []
        self._mpv_gen = 0              # generation counter
        self._native_active_context: NativeContext | None = None
        self._pending_native_context: NativeContext | None = None
        self._native_track_observers: list[tuple[str, Any]] = []
        self._native_playlist_contexts: dict[int, NativeContext] = {}
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
        # Starvation fault gate: a track that keeps running the cache dry is a
        # serve-side problem, not a track worth stuttering through. Per-track
        # counters reset in _begin_track; crossing the thresholds makes
        # _handle_buffering advance past the resource (2026-08-08 soak: repeat
        # offenders like f3v0r_gimme_1 froze 15x before finishing).
        self._starvation_track_events = 0
        self._starvation_track_total_s = 0.0
        self._starvation_fault_scheduled = False
        self._last_seek_ts = 0.0
        self._buffering_card = False
        self._retry_count = 0
        self._force_transcode = False
        self._force_software_decode = False
        self._decoder_fault_count = 0
        self._decoder_hardware_attempts = 0
        self._decoder_hardware_successes = 0
        self._decoder_software_fallbacks = 0
        self._decoder_recovery_exhausted = 0
        self._decoder_quarantines = 0
        self._decoder_hardware_success_recorded = False
        self._decoder_recovery_scheduled = False
        self._decoder_recovery_token: PlaybackToken | None = None
        self._transport_retry_count = 0
        self._transport_recovery_scheduled = False
        self._transport_recovery_token: PlaybackToken | None = None
        self._retry_backoff_token: PlaybackToken | None = None
        self._park_token: PlaybackToken | None = None
        self._transport_resource_quarantined = False
        self._resource_quarantined = False
        self._closing = False
        # URL state used by the controller's transcode-concurrency gate.
        # Prefetched URLs are kept separate from the currently playing URL so
        # a warm future HLS playlist is not counted as active server work.
        self._stream_url: str | None = None
        self._prefetched_stream_url: str | None = None
        self._prefetched_playback_plan: PlaybackPlan | None = None
        self._played_anything = False
        self._paused = False  # main-thread cache; safe to read cross-thread
        self._last_next_request_ts = 0.0
        self._pending_next = False  # a throttled advance waiting to re-fire
        self._pending_next_token: PlaybackToken | None = None
        self._prefetch_request_token: PlaybackToken | None = None
        self._mouse_in_cell = False
        self._emby_session_id: str | None = None
        self._emby_item_id: str | None = None
        # True between a reuse-loadfile and the old track's stale end-file
        # (reason "stop"). NEVER set for a fresh mpv — no stale event will
        # arrive to clear it, and a latched _switching once silently disabled
        # the eof handling of every cell's first track (2026-07-11 lockup).
        self._switching = False
        self._track_done = False  # this track already triggered its advance
        self._eof_reached = False
        self._cache_buffering_state: Any = "?"
        self._audio_started = False  # True once this track's audio is armed
        # Audio track selection and keyframe relock can block inside libmpv
        # while an HLS demuxer catches up. They must never occupy the Qt GUI
        # thread: the audio-focused soak observed >200ms mute slots and
        # multi-second wall stalls. One daemon worker per cell is enough; a
        # token + event prevent stale work from racing a track switch or
        # teardown.
        self._audio_arm_lock = threading.Lock()
        self._audio_arm_call_lock = threading.Lock()
        self._audio_arm_token = 0
        self._audio_arm_inflight_token: int | None = None
        self._audio_arm_done = threading.Event()
        self._audio_arm_done.set()
        self._audio_arm_pending_enabled: bool | None = None
        # Latest-value native control writes are serialized with audio arm and
        # run off the GUI thread. Tokens discard stale mute/volume writes after
        # a track replacement or shutdown.
        self._native_control_tokens: dict[str, int] = {}
        self._native_control_serial = 0
        self._deferred_play: tuple[dict[str, Any], str, bool, Any | None, str | None, PlaybackPlan | None] | None = None
        self._deferred_play_retry_scheduled = False
        self._track_generation = 0
        # (item, url, emby_session_id) queued on the live mpv playlist so
        # prefetch-playlist warms its demuxer before the current track ends.
        self._prefetched: tuple[dict[str, Any], str, str] | None = None
        self._prefetch_drop_retry_count = 0
        self._prefetch_drop_retry_scheduled = False
        # macOS playlist advance is a native IPC operation that can block for
        # hundreds of milliseconds while the warm demuxer switches. Keep the
        # request identity separate from playback generations so a queued
        # worker can be invalidated by a new play or shutdown.
        self._prefetch_advance_serial = 0
        self._prefetch_advance_inflight: int | None = None
        self._prefetch_advance_token: PlaybackToken | None = None
        self._prefetch_advance_pending: tuple[dict[str, Any], str, str] | None = None
        # Parser logs can arrive after a malformed queued item has been
        # removed. Suppress only that short tail so it cannot blame the live
        # track; admitting a new prefetch clears the window.
        self._prefetch_fault_suppression_until = 0.0
        # Reused macOS loadfile runs off the GUI thread; the request token
        # rejects stale workers after replacement or shutdown.
        self._async_play_serial = 0
        self._async_play_inflight: int | None = None
        self._async_play_pending: tuple[int, PlaybackToken, Any, str, Any | None] | None = None

        # Reliability / self-healing (Epic 2)
        self._last_progress_ts = 0.0   # monotonic ts of last time-pos advance
        self._last_seen_pos = -1.0     # last observed time-pos value
        self._failure_ts: deque[float] = deque(maxlen=64)  # recent failure times
        self._parked = False           # crash-loop parked → stop retrying
        # Budgeted mpv opts, set by the controller once the grid size is known
        # (memory-aware demuxer cache). None → fall back to unbudgeted defaults.
        self._mpv_opts: dict[str, Any] | None = None

        # Stats
        # Observer callbacks run on mpv's event thread while shutdown and
        # telemetry snapshots run on the GUI thread. Keep snapshots coherent
        # without holding the native-call ownership lock during normal reads.
        self._stats_lock = threading.Lock()
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
        self._sig_decoder_fault.connect(
            self._handle_decoder_fault, Qt.ConnectionType.QueuedConnection
        )
        self._sig_transport_fault.connect(
            self._handle_transport_fault, Qt.ConnectionType.QueuedConnection
        )
        self._sig_prefetch_fault.connect(
            self._handle_prefetch_fault, Qt.ConnectionType.QueuedConnection
        )
        self._sig_prefetched_advance.connect(
            self._finish_prefetched_advance, Qt.ConnectionType.QueuedConnection
        )
        self._sig_play_finished.connect(
            self._finish_async_play, Qt.ConnectionType.QueuedConnection
        )
        self._sig_release_retry.connect(
            self._retry_destroy_on_gui, Qt.ConnectionType.QueuedConnection
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
        if self._mpv is not None or self._closing:
            return
        import mpv as _mpv

        if not self.video_frame.isVisible():
            logger.warning("video_frame not visible — deferring mpv creation.")
            return

        _opts = dict(self._mpv_opts or apply_env_overrides(MPV_OPTS))
        if self._force_software_decode:
            # Decoder fallback is isolated to this cell and this media item;
            # the wall's other cells retain their configured hardware path.
            _opts["hwdec"] = "no"
        _requested_hwdec = str(_opts.get("hwdec", "no")).strip().lower()
        if _requested_hwdec not in {"", "no", "none", "software", "false", "0"}:
            with self._stats_lock:
                self._decoder_hardware_attempts += 1
                self._decoder_hardware_success_recorded = False
        next_gen = self._mpv_gen + 1

        def _log_handler(level: str, component: str, message: str) -> None:
            # mpv log callbacks carry no playlist/resource identity. Keep the
            # diagnostic log, but never attribute a decoder/transport fault to
            # whichever track happens to be active when a delayed log arrives.
            self._mpv_log(level, component, message, next_gen, None, None)

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
                    log_handler=_log_handler,
                    **_opts,
                )
            else:
                m = _mpv.MPV(
                    wid=str(wid),
                    log_handler=_log_handler,
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
            self._render_context_released = False

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
        # New instances always start with lazy audio, including a cell that
        # is currently unmuted. The first post-load play() call arms audio
        # through _enable_audio_track, so recreation never performs aid=auto
        # synchronously during the GUI-side constructor path.
        self._audio_started = False
        try:
            m["aid"] = "no"
        except Exception as e:
            logger.debug("mpv: failed to set initial aid: %s", e)
        if self.looping:
            try:
                m["loop-file"] = "inf"
            except Exception as e:
                logger.debug("mpv: failed to set initial loop-file: %s", e)

        self._mpv_gen += 1
        gen = self._mpv_gen
        self._native_active_context = None
        self._native_playlist_contexts.clear()
        self._decoder_recovery_scheduled = False
        self._decoder_recovery_token = None
        self._transport_recovery_scheduled = False
        self._transport_recovery_token = None

        @m.event_callback("start-file")
        def _on_start_file(ev: Any) -> None:
            if gen != self._mpv_gen:
                return
            entry_id = self._native_playlist_entry_id(ev)
            context = (
                self._native_playlist_contexts.get(entry_id)
                if entry_id is not None
                else None
            )
            pending = self._pending_native_context
            if context is not None:
                # A delayed event for an earlier playlist entry must never
                # rebind observers to the replacement track. Reuse of the
                # same native entry for a new generation is the one exception:
                # the pending item/URL prove that this is the new admission.
                if context[1] != self._track_generation:
                    if (
                        pending is None
                        or pending[2:4] != context[2:4]
                    ):
                        return
                    context = pending
                    if entry_id is not None:
                        self._native_playlist_contexts[entry_id] = context
            else:
                if pending is None or pending[0] != gen:
                    return
                context = pending
                if entry_id is not None:
                    self._native_playlist_contexts[entry_id] = context
                    while len(self._native_playlist_contexts) > 32:
                        self._native_playlist_contexts.pop(
                            next(iter(self._native_playlist_contexts)),
                            None,
                        )
            self._native_active_context = context
            self._playback_controller.transition(
                PlaybackEvent.LOAD_STARTED,
                self._playback_state_identity(context),
            )
            if pending is not None and context == pending:
                self._pending_native_context = None
            self._switching = False
            self._eof_reached = False
            self._bind_native_track_observers(m, gen, context)

        @m.event_callback("end-file")
        def _on_end_file(ev: Any) -> None:
            context = self._native_context_for_event(ev, gen)
            if context is None or context[1] != self._track_generation:
                return
            reason = end_file_reason(ev)
            if reason == "error":
                error_code = getattr(getattr(ev, "data", None), "error", None)
                message = f"mpv end-file error: {error_code}"
                if error_code in {-15, -17, -18}:
                    self._sig_decoder_fault.emit(context, message)
                else:
                    self._sig_transport_fault.emit(context, message)
            elif reason not in ("stop", "quit", "redirect", "restarted"):
                self._sig_eof.emit(context, reason)

        # Property observers are bound per start-file in
        # _bind_native_track_observers(), where each callback closes over the
        # immutable resource context that produced it.
        self._mpv = m

    @staticmethod
    def _native_playlist_entry_id(event: Any) -> int | None:
        data = getattr(event, "data", event)
        value = (
            data.get("playlist_entry_id")
            if isinstance(data, dict)
            else getattr(data, "playlist_entry_id", None)
        )
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _native_context_for_event(
        self, event: Any, gen: int,
    ) -> NativeContext | None:
        entry_id = self._native_playlist_entry_id(event)
        context = (
            self._native_playlist_contexts.get(entry_id)
            if entry_id is not None
            else None
        )
        return context if context is not None and context[0] == gen else None

    def _unbind_native_track_observers(self, mpv_ref: Any | None = None) -> None:
        mpv_ref = mpv_ref or self._mpv
        for prop, handler in self._native_track_observers:
            if mpv_ref is not None:
                try:
                    mpv_ref.unobserve_property(prop, handler)
                except Exception:
                    pass
        self._native_track_observers.clear()

    def _record_hwdec_current(self, value: Any) -> None:
        """Store the active decoder and count the first hardware activation."""
        if isinstance(value, bytes):
            normalized = value.decode("utf-8", "replace")
        else:
            normalized = str(value)
        is_hardware = normalized.strip().lower() not in {
            "", "no", "none", "software", "false", "0",
        }
        with self._stats_lock:
            self._stats_info["hwdec-current"] = normalized
            if (
                is_hardware
                and self._decoder_hardware_attempts > 0
                and not self._decoder_hardware_success_recorded
            ):
                self._decoder_hardware_successes += 1
                self._decoder_hardware_success_recorded = True

    def _bind_native_track_observers(
        self, mpv_ref: Any, gen: int, context: NativeContext,
    ) -> None:
        self._unbind_native_track_observers(mpv_ref)

        def register(prop: str, handler: Any) -> None:
            try:
                bound = mpv_ref.property_observer(prop)(handler)
                self._native_track_observers.append((prop, bound))
            except Exception as e:
                logger.debug("Could not bind track observer %s: %s", prop, e)

        def on_eof(_name: str, value: Any) -> None:
            if value is True and self._native_context_is_current(context):
                self._eof_reached = True
                self._sig_track_done.emit(context)

        def on_pfc(_name: str, value: Any) -> None:
            if value is not None and self._native_context_is_current(context):
                self._cache_buffering_state = value
                self._sig_buffering.emit(context, bool(value))

        def on_time(_name: str, value: float | None) -> None:
            if value is None or not self._native_context_is_current(context):
                return
            if value > self._last_seen_pos:
                self._last_seen_pos = value
                self._last_progress_ts = _time.monotonic()
            self._play_pos = value
            if value > 0.02 and not self._played_anything:
                self._played_anything = True
            if self._duration_s > 0 and self._duration_s < 0.5 and value > 0:
                self._played_anything = True

        def on_duration(_name: str, value: float | None) -> None:
            if value and self._native_context_is_current(context):
                self._duration_s = float(value)

        def on_hwdec_current(_name: str, value: Any) -> None:
            if value is not None and self._native_context_is_current(context):
                self._record_hwdec_current(value)

        register("eof-reached", on_eof)
        register("paused-for-cache", on_pfc)
        register("time-pos", on_time)
        register("duration", on_duration)
        register("hwdec-current", on_hwdec_current)
        if STATS_ENABLED:
            for prop in STATS_COUNTER_PROPS:
                def on_counter(
                    _name: str, value: float | None,
                    prop: str = prop, context: NativeContext = context,
                ) -> None:
                    if value is not None and self._native_context_is_current(context):
                        with self._stats_lock:
                            self._stats_current[prop] = float(value)
                register(prop, on_counter)
            for prop in STATS_INFO_PROPS:
                if prop == "hwdec-current":
                    continue
                def on_info(
                    _name: str, value: Any,
                    prop: str = prop, context: NativeContext = context,
                ) -> None:
                    if value is not None and self._native_context_is_current(context):
                        with self._stats_lock:
                            self._stats_info[prop] = value
                register(prop, on_info)

    def _native_context_is_current(self, context: NativeContext) -> bool:
        current_id = (self.current_item or {}).get("Id")
        active = self._native_active_context
        pending = self._pending_native_context
        session_matches = (
            context[4] is None
            or context[4] == self._emby_session_id
            or (
                active is not None
                and active[0:2] == context[0:2]
                and active[4] == context[4]
            )
            or (
                pending is not None
                and pending[0:2] == context[0:2]
                and pending[4] == context[4]
            )
        )
        return (
            not self._closing
            and self._mpv is not None
            and context[0] == self._mpv_gen
            and context[1] == self._track_generation
            and context[2] == current_id
            and context[3] == self._stream_url
            and session_matches
        )

    def _native_call(
        self,
        fn: Any,
        *,
        audio_lock_held: bool = False,
        retry: Any | None = None,
        valid: Any | None = None,
    ) -> bool:
        """Run one native mpv operation under the shared ownership lock."""
        if self._mpv is None:
            return False
        operation_token = self._current_playback_token()

        def _schedule_retry() -> None:
            if retry is None or self._closing:
                return

            def _retry_if_current() -> None:
                if self._closing or self._mpv is None:
                    return
                if operation_token is None:
                    return
                if not self._playback_token_is_current(operation_token):
                    return
                if valid is not None and not valid():
                    return
                retry()

            QTimer.singleShot(50, _retry_if_current)

        if audio_lock_held:
            try:
                if valid is not None and not valid():
                    return False
                fn(self._mpv)
                return True
            except Exception as e:
                logger.debug("native mpv call failed: %s", e)
                return False
        if not self._audio_arm_call_lock.acquire(blocking=False):
            _schedule_retry()
            return False
        try:
            if self._mpv is None:
                return False
            if valid is not None and not valid():
                return False
            fn(self._mpv)
            return True
        except Exception as e:
            logger.debug("native mpv call failed: %s", e)
            return False
        finally:
            self._audio_arm_call_lock.release()

    def _stop_mpv_for_render_release(self) -> None:
        """Stop the VO before freeing a macOS libmpv render context."""
        if self._mpv is None:
            return
        if (
            sys.platform == "darwin"
            and getattr(self.video_frame, "_ctx", None) is None
        ):
            # The wall may already have performed the GUI-thread pre-release;
            # do not send a second stop after the render context is gone.
            return
        try:
            self._mpv["mute"] = True
        except Exception:
            pass
        if sys.platform == "darwin":
            try:
                # render_context_free() disables an active VO. Stop first so
                # the core does not continue submitting frames after the
                # context has been freed (the soak logged "No render context
                # set" during the old shutdown ordering).
                self._mpv.command("stop")
            except Exception as e:
                logger.debug("mpv stop before render release failed: %s", e)

    def _release_render_context_on_gui(self) -> bool:
        """Release the macOS render context only from the widget's thread."""
        if sys.platform != "darwin" or self._render_context_released:
            return True
        if self._mpv is None:
            self._render_context_released = True
            return True
        try:
            self._stop_mpv_for_render_release()
            self.video_frame.release()
            self._render_context_released = True
            return True
        except Exception as e:
            logger.debug("GL render-context release failed: %s", e)
            return False

    def request_render_release_when_idle(
        self, shutdown_deadline: float | None = None,
    ) -> None:
        """Retry GUI render release after an in-flight native call drains."""
        if sys.platform != "darwin" or self._render_context_released:
            return
        self._shutdown_render_release_requested = True
        candidate = (
            shutdown_deadline
            if shutdown_deadline is not None
            else _time.monotonic() + 5.0
        )
        if self._shutdown_render_release_deadline <= 0.0:
            self._shutdown_render_release_deadline = candidate
        else:
            self._shutdown_render_release_deadline = min(
                self._shutdown_render_release_deadline, candidate,
            )
        QTimer.singleShot(0, self._release_render_context_when_idle)

    def _release_render_context_when_idle(self) -> None:
        if (
            not self._shutdown_render_release_requested
            or self._render_context_released
            or not self._closing
        ):
            return
        if _time.monotonic() >= self._shutdown_render_release_deadline:
            logger.error(
                "Bounded GUI render release deadline reached; retaining "
                "render finalizer record."
            )
            self._render_finalizer_pending = True
            self._shutdown_render_release_requested = False
            return
        if not self._audio_arm_call_lock.acquire(blocking=False):
            QTimer.singleShot(25, self._release_render_context_when_idle)
            return
        try:
            released = self._release_render_context_on_gui()
        finally:
            self._audio_arm_call_lock.release()
        if not released:
            QTimer.singleShot(25, self._release_render_context_when_idle)
        else:
            self._shutdown_render_release_requested = False
            self._render_finalizer_pending = False

    def has_pending_render_finalizer(self) -> bool:
        return self._render_finalizer_pending

    def _retry_destroy_on_gui(self) -> None:
        if not self._destroy_retry_requested:
            return
        if self._mpv is None:
            self._destroy_retry_requested = False
            return
        if _time.monotonic() >= self._destroy_retry_deadline:
            logger.error("Bounded native destroy deadline reached; abandoning safely.")
            self._destroy_retry_requested = False
            return
        if not self._audio_arm_call_lock.acquire(blocking=False):
            QTimer.singleShot(25, self._retry_destroy_on_gui)
            return
        remaining = max(
            0.0, self._destroy_retry_deadline - _time.monotonic(),
        )
        try:
            self._destroy_mpv_impl(wait_s=remaining)
        finally:
            self._audio_arm_call_lock.release()
        if self._mpv is not None:
            QTimer.singleShot(25, self._retry_destroy_on_gui)
        else:
            self._destroy_retry_requested = False

    def has_pending_native_finalizer(self) -> bool:
        with self._native_finalizer_lock:
            return bool(self._native_finalizer_records)

    def _destroy_mpv(
        self, wait_s: float = 0.75, *, audio_lock_held: bool = False,
        shutdown_deadline: float | None = None,
    ) -> None:
        """Terminate mpv while serializing against audio-arm native calls."""
        if audio_lock_held:
            self._destroy_mpv_impl(wait_s)
            return
        remaining = (
            max(0.0, shutdown_deadline - _time.monotonic())
            if shutdown_deadline is not None
            else 0.25
        )
        if not self._audio_arm_call_lock.acquire(timeout=min(0.25, remaining)):
            logger.warning("mpv release deferred: native audio call is still busy.")
            if not self._destroy_retry_requested:
                self._destroy_retry_requested = True
                self._destroy_retry_deadline = (
                    shutdown_deadline
                    if shutdown_deadline is not None
                    else _time.monotonic() + 5.0
                )
            self._sig_release_retry.emit()
            return
        try:
            self._destroy_mpv_impl(wait_s)
        finally:
            self._audio_arm_call_lock.release()

    def _destroy_mpv_impl(self, wait_s: float = 1.5) -> None:
        """Terminate mpv with a genuinely bounded wait.

        terminate runs on a daemon thread; we join for at most `wait_s` and
        then truly abandon it. The previous ThreadPoolExecutor version looked
        bounded but wasn't: exiting its `with` block calls shutdown(wait=True),
        which blocks the GUI thread until terminate returns — one wedged libmpv
        teardown froze every cell on the wall indefinitely.
        """
        self._cancel_audio_arm(timeout_s=0.0)
        if self._mpv is None:
            return
        if STATS_ENABLED:
            self._flush_stats(audio_lock_held=True)
        if sys.platform == "darwin":
            # The render context belongs to the GUI thread. A worker may only
            # destroy the core after the GUI has released it; otherwise leave
            # the core abandoned rather than touching Qt/GL off-thread.
            if not self._render_context_released:
                if QThread.currentThread() is not self.thread():
                    logger.warning(
                        "Skipping off-thread mpv destroy before GUI render release."
                    )
                    return
                if not self._release_render_context_on_gui():
                    return
        # Silence the handle BEFORE terminate: a wedged teardown gets
        # abandoned on a daemon thread below, and an abandoned-but-alive
        # instance that was audible would keep playing sound that no
        # control can reach (its cell now talks to the replacement).
        try:
            self._mpv["mute"] = True
        except Exception:
            pass
        mpv_ref = self._mpv
        self._unbind_native_track_observers(mpv_ref)
        self._mpv = None
        record: dict[str, Any] = {
            "mpv": mpv_ref,
            "status": "running",
            "started_at": _time.monotonic(),
        }
        with self._native_finalizer_lock:
            self._native_finalizer_records.append(record)

        def _terminate() -> None:
            try:
                mpv_ref.terminate()
                record["status"] = "finished"
            except Exception as e:
                record["status"] = "failed"
                logger.debug("mpv terminate raised: %s", e)
            finally:
                if record["status"] == "finished":
                    with self._native_finalizer_lock:
                        self._native_finalizer_records[:] = [
                            item for item in self._native_finalizer_records
                            if item is not record
                        ]
                else:
                    logger.error(
                        "mpv finalizer did not confirm termination; retaining "
                        "the durable record."
                    )

        t = threading.Thread(
            target=_terminate, name="mpv-terminate", daemon=True
        )
        record["thread"] = t
        try:
            t.start()
        except Exception as e:
            record["status"] = "failed-to-start"
            logger.error("mpv finalizer could not start: %s", e)
            return
        if wait_s > 0:
            t.join(wait_s)
            if t.is_alive():
                logger.warning(
                    "mpv terminate still running after %.1fs — retaining "
                    "durable finalizer record.", wait_s
                )


    def _flush_stats(
        self, *, audio_lock_held: bool = False, timeout_s: float = 0.25,
    ) -> None:
        """Snapshot current mpv stats into running totals."""
        if not audio_lock_held:
            if not self._audio_arm_call_lock.acquire(timeout=max(0.0, timeout_s)):
                logger.debug("Stats flush skipped while native audio call is busy.")
                return
            try:
                self._flush_stats(audio_lock_held=True, timeout_s=timeout_s)
            finally:
                self._audio_arm_call_lock.release()
            return
        if self._mpv is not None:
            # Stats names are property-only: read via attribute (m[...] is
            # options/<name> and raises), or the snapshot silently no-ops and
            # the totals ride on whatever the observers last delivered.
            for prop in STATS_COUNTER_PROPS:
                try:
                    v = getattr(self._mpv, prop.replace("-", "_"))
                    if v is not None:
                        with self._stats_lock:
                            self._stats_current[prop] = float(v)
                except Exception:
                    pass
            for prop in STATS_INFO_PROPS:
                try:
                    v = getattr(self._mpv, prop.replace("-", "_"))
                    if v is not None:
                        if prop == "hwdec-current":
                            self._record_hwdec_current(v)
                        else:
                            with self._stats_lock:
                                self._stats_info[prop] = v
                except Exception:
                    pass
        # Detach-swap before iterating: the stats observers write these
        # dicts from the mpv event thread, and iterating a dict that grows
        # mid-iteration raises. The reference swap is GIL-atomic; observers
        # write to the fresh dict while we drain the detached one.
        with self._stats_lock:
            current, self._stats_current = self._stats_current, {}
            for k, v in current.items():
                self._stats_total[k] = self._stats_total.get(k, 0.0) + v

    def _stats_snapshot(self) -> dict[str, dict[str, Any]]:
        """Return coherent cumulative stats without a native property read."""
        with self._stats_lock:
            totals = dict(self._stats_total)
            for key, value in self._stats_current.items():
                totals[key] = totals.get(key, 0.0) + value
            return {"totals": totals, "info": dict(self._stats_info)}

    def telemetry_snapshot(
        self, *, reset_interval: bool = False,
    ) -> dict[str, Any]:
        """Return render, native-stat, and audio state for diagnostics."""
        render_snapshot: dict[str, Any] = {}
        snapshot_fn = getattr(self.video_frame, "telemetry_snapshot", None)
        if callable(snapshot_fn):
            candidate = snapshot_fn(reset_interval=reset_interval)
            if isinstance(candidate, dict):
                render_snapshot = candidate
        stats = self._stats_snapshot()
        item = self.current_item or {}
        info = stats.get("info", {})
        if not isinstance(info, dict):
            info = {}
        requested_decoder = (
            self._playback_plan.client_decoder
            if self._playback_plan is not None else None
        )
        return {
            "render": render_snapshot,
            "stats": stats,
            "audio": {
                "muted": bool(self.muted),
                "audio_started": bool(self._audio_started),
            },
            "decoder": {
                "requested": requested_decoder,
                "active": info.get("hwdec-current"),
                "fault_count": max(0, int(self._decoder_fault_count)),
                "hardware_attempts": max(0, int(self._decoder_hardware_attempts)),
                "hardware_successes": max(0, int(self._decoder_hardware_successes)),
                "software_fallbacks": max(0, int(self._decoder_software_fallbacks)),
                "recovery_exhausted": max(0, int(self._decoder_recovery_exhausted)),
                "quarantines": max(0, int(self._decoder_quarantines)),
                "software_fallback": bool(self._force_software_decode),
                "resource_quarantined": bool(self._resource_quarantined),
            },
            "item_id": item.get("Id"),
            "item_name": item.get("Name"),
        }

    def _mpv_log(
        self,
        level: str,
        component: str,
        message: str,
        generation: int | None = None,
        track_generation: int | None = None,
        resource_context: NativeContext | None = None,
    ) -> None:
        """Route mpv logs using the immutable producer-side resource context."""
        text = message.strip()
        if level == "warn" and any(pat in text for pat in MPV_LOG_NOISE):
            return
        msg = f"mpv[{component}] {text}"
        if level in ("fatal", "error"):
            logger.error(msg)
        elif level == "warn":
            logger.warning(msg)
        fault = classify_playback_fault(text)
        if fault == "other" or self._closing:
            return
        gen = self._mpv_gen if generation is None else generation
        if (
            resource_context is None
            and fault == "decoder"
            and is_malformed_stream_fault(text)
            and _time.monotonic() < self._prefetch_fault_suppression_until
        ):
            return
        if resource_context is None:
            pending = self._prefetched
            prefetch_context = (
                (
                    self._mpv_gen,
                    self._track_generation + 1,
                    pending[0].get("Id"),
                    pending[1],
                    pending[2],
                )
                if pending is not None else None
            )
            queued_context = context_for_prefetch_fault(
                fault, text, prefetch_context,
                generation=gen, switching=self._switching,
            )
            if queued_context is not None:
                try:
                    self._sig_prefetch_fault.emit(queued_context, text)
                except Exception:
                    pass
                return
            context = context_for_unscoped_fault(
                fault,
                self._native_active_context,
                generation=gen,
                switching=self._switching,
            )
            if context is not None:
                track_generation = context[1]
        else:
            context = resource_context
        if (
            context is None
            or context[0] != gen
            or (
                track_generation is not None
                and context[1] != track_generation
            )
        ):
            return
        try:
            if fault == "decoder":
                self._sig_decoder_fault.emit(context, text)
            elif fault == "transport":
                self._sig_transport_fault.emit(context, text)
        except Exception:
            # Log callbacks run on native mpv threads and must never propagate
            # into the callback boundary during QObject teardown.
            pass


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

    def _begin_track(
        self,
        item: dict[str, Any],
        *,
        preserve_failure_state: bool = False,
    ) -> None:
        """Per-track state + UI reset shared by play() and the prefetched
        advance. No title overlay here: the wall stays chrome-free during
        steady playback (title lives in the hover control bar)."""
        if not preserve_failure_state:
            self._retry_count = 0
            self._force_transcode = False
            self._force_software_decode = False
            self._decoder_fault_count = 0
            self._decoder_hardware_attempts = 0
            self._decoder_hardware_successes = 0
            self._decoder_software_fallbacks = 0
            self._decoder_recovery_exhausted = 0
            self._decoder_quarantines = 0
            self._decoder_hardware_success_recorded = False
            self._transport_retry_count = 0
            self._decoder_recovery_scheduled = False
            self._transport_recovery_scheduled = False
            self._transport_resource_quarantined = False
            self._resource_quarantined = False
            self._starvation_track_events = 0
            self._starvation_track_total_s = 0.0
            self._starvation_fault_scheduled = False
        self._pending_next = False
        self._pending_next_token = None
        self._prefetch_request_token = None
        # Recovery timers belong to the exact resource, even when retry
        # counters are deliberately preserved for a same-URL reload.
        self._decoder_recovery_scheduled = False
        self._decoder_recovery_token = None
        self._transport_recovery_scheduled = False
        self._transport_recovery_token = None
        self._retry_backoff_token = None
        self._park_token = None
        self.current_item = item
        self._track_generation += 1
        self._eof_reached = False
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

    def _invalidate_async_play(self) -> None:
        pending = self._async_play_pending
        if self._async_play_inflight is None and pending is None:
            return
        self._async_play_serial += 1
        self._async_play_inflight = None
        self._async_play_pending = None
        if pending is not None and pending[4] is not None:
            pending[4](False)

    def _queue_async_play(
        self, token: PlaybackToken, url: str, on_started: Any | None,
    ) -> bool:
        if self._closing or self._mpv is None:
            return False
        mpv_ref = self._mpv
        self._async_play_serial += 1
        request_id = self._async_play_serial
        self._async_play_inflight = request_id
        self._async_play_pending = (
            request_id, token, mpv_ref, url, on_started,
        )
        worker = threading.Thread(
            target=self._async_play_worker,
            args=(request_id, mpv_ref, token, url),
            name="mpv-async-play",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as e:
            self._async_play_inflight = None
            self._async_play_pending = None
            logger.warning("Async play worker could not start: %s", e)
            return False
        return True

    def _async_play_is_current(
        self, request_id: int, mpv_ref: Any, token: PlaybackToken,
    ) -> bool:
        return (
            not self._closing
            and self._async_play_inflight == request_id
            and self._mpv is mpv_ref
            and self._current_playback_token() == token
        )

    def _async_play_worker(
        self, request_id: int, mpv_ref: Any, token: PlaybackToken, url: str,
    ) -> None:
        succeeded = False
        try:
            with self._audio_arm_call_lock:
                if not self._async_play_is_current(request_id, mpv_ref, token):
                    return
                mpv_ref["mute"] = self.muted
                mpv_ref.command("loadfile", url)
                mpv_ref["pause"] = False
                succeeded = True
        except Exception as e:
            logger.warning("Async play loadfile failed: %s", e)
        finally:
            try:
                self._sig_play_finished.emit(request_id, succeeded)
            except Exception:
                pass

    def _finish_async_play(self, request_id: int, succeeded: bool) -> None:
        if self._async_play_inflight != request_id:
            return
        pending = self._async_play_pending
        if pending is None:
            return
        self._async_play_inflight = None
        self._async_play_pending = None
        _, token, mpv_ref, url, on_started = pending
        current = (
            not self._closing
            and self._mpv is mpv_ref
            and self._current_playback_token() == token
        )
        if not current:
            if on_started is not None:
                on_started(False)
            return
        if not succeeded:
            self._switching = False
            failed_context: NativeContext = self._pending_native_context or (
                self._mpv_gen, self._track_generation, token.item_id, url,
                self._emby_session_id,
            )
            self._sig_eof.emit(failed_context, "error")
            if on_started is not None:
                on_started(False)
            return
        self._forget_prefetch_after_native_clear(requeue=True)
        self._paused = False
        self.btn_play.setText(_G_PAUSE)
        if not self.muted and not self._audio_started:
            self._enable_audio_track()
        if on_started is not None:
            on_started(True)


    def _defer_play_until_audio_idle(
        self,
        item: dict[str, Any],
        url: str,
        preserve_failure_state: bool = False,
        on_started: Any | None = None,
        session_id: str | None = None,
        playback_plan: PlaybackPlan | None = None,
    ) -> None:
        """Retry a transition after a worker releases the native-call lock."""
        previous = self._deferred_play
        superseded = (
            previous is not None
            and (
                previous[0] is not item
                or previous[1] != url
                or previous[4] != session_id
            )
        )
        if superseded and previous[3] is not None:
            previous[3](False)
        self._deferred_play = (
            item, url, preserve_failure_state, on_started, session_id,
            playback_plan,
        )
        if self._deferred_play_retry_scheduled:
            return
        self._deferred_play_retry_scheduled = True

        def _retry() -> None:
            self._deferred_play_retry_scheduled = False
            pending = self._deferred_play
            if self._closing or pending is None or self._mpv is None:
                self._deferred_play = None
                if pending is not None and pending[3] is not None:
                    pending[3](False)
                return
            if self._audio_arm_call_lock.locked():
                self._defer_play_until_audio_idle(*pending)
                return
            self._deferred_play = None
            self.play(
                pending[0], pending[1],
                preserve_failure_state=pending[2],
                on_started=pending[3],
                session_id=pending[4],
                playback_plan=pending[5],
            )

        QTimer.singleShot(50, _retry)

    @traced("cell.play")
    def play(
        self,
        item: dict[str, Any],
        url: str,
        *,
        preserve_failure_state: bool = False,
        on_started: Any | None = None,
        session_id: str | None = None,
        playback_plan: PlaybackPlan | None = None,
    ) -> bool:
        if self._closing:
            if on_started is not None:
                on_started(False)
            return False
        if not self._audio_arm_call_lock.acquire(blocking=False):
            if sys.platform == "darwin":
                self._invalidate_async_play()
            self._defer_play_until_audio_idle(
                item, url, preserve_failure_state, on_started, session_id,
                playback_plan,
            )
            return True  # admitted for deferred execution
        started: bool | None = False
        try:
            # The worker either owns the call lock (in which case we returned
            # above) or is waiting to acquire it. Invalidate it before the
            # replacement and let the generation check discard it afterward.
            self._cancel_audio_arm(timeout_s=0.0)
            started = self._play_impl(
                item, url, preserve_failure_state=preserve_failure_state,
                on_started=on_started,
                session_id=session_id,
                playback_plan=playback_plan,
            )
        finally:
            self._audio_arm_call_lock.release()
        if on_started is not None and started is not None:
            on_started(started)
        return True if started is None else started


    def _play_impl(
        self,
        item: dict[str, Any],
        url: str,
        *,
        preserve_failure_state: bool = False,
        on_started: Any | None = None,
        session_id: str | None = None,
        playback_plan: PlaybackPlan | None = None,
    ) -> bool | None:
        """Load a video into this cell."""
        if self._closing:
            return False
        if self._parked:
            # A manual/web advance during the park cooldown is a deliberate
            # resume: clear the parked state so a failure of THIS load runs
            # the normal retry chain instead of being swallowed by the
            # _on_error parked-guard (2026-07-13 audit). The pending unpark
            # timer no-ops (it checks _parked).
            self._parked = False
            self._failure_ts.clear()
            logger.info("Parked cell manually resumed.")
        self._invalidate_async_play()
        if playback_plan is not None:
            self._playback_plan = playback_plan
        # A regular replacement supersedes any queued asynchronous playlist
        # advance. The worker remains daemonized but its identity check will
        # reject the old native handle/state before it can commit.
        self._invalidate_prefetched_advance()
        # loadfile (replace) clears the mpv playlist tail, so any queued
        # prefetch entry is gone with it (probed live 2026-07-13).
        same_resource = (
            self.current_item is item and self._stream_url == url
        )
        preserve_failure_state = preserve_failure_state or same_resource
        self.drop_prefetch(audio_lock_held=True)
        self._stream_url = url
        self._prefetched_stream_url = None
        self._begin_track(
            item, preserve_failure_state=preserve_failure_state,
        )

        # Determine if we need to recreate mpv
        need_create = (
            self._mpv is None
            or self._force_transcode
            or self._force_software_decode
        )
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
            self._destroy_mpv(audio_lock_held=True)
            self._ensure_mpv()
        elif self._mpv is not None:
            # Reusing the instance: loadfile resets mpv's per-file counters,
            # so bank the outgoing track's stats now or they're lost.
            if STATS_ENABLED:
                self._flush_stats(audio_lock_held=True)
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
            return False

        # _switching suppresses stale events from the track loadfile replaces:
        # the old track's end-file (reason "stop") and any in-flight
        # eof-reached signal. Only a REUSED mpv has a track to replace — on a
        # fresh instance no stale event will ever arrive, and setting the flag
        # there latches it forever (watchdog + advance both gated on it).
        self._switching = not need_create
        self._track_done = False
        self._pending_native_context = (
            self._mpv_gen,
            self._track_generation,
            item.get("Id"),
            url,
            session_id or self._emby_session_id,
        )
        self._playback_controller.transition(
            PlaybackEvent.LOAD_REQUESTED,
            self._playback_state_identity(self._pending_native_context),
        )
        if sys.platform == "darwin":
            token = self._current_playback_token()
            if token is None or not self._queue_async_play(token, url, on_started):
                self._switching = False
                return False
            return None
        try:
            self._mpv["mute"] = self.muted
            self._mpv.command("loadfile", url)
            self._forget_prefetch_after_native_clear(requeue=True)
            # mpv emits the outgoing end-file callbacks while this command is
            # still executing. Keep the old native context until command
            # completion, then bind subsequent callbacks to this new resource.
            # Keep the old context until the matching start-file event.
            # keep-open pauses the player at EOF and the pause property
            # PERSISTS across loadfile (probed live 2026-07-12) — without an
            # explicit unpause every post-EOF load sits frozen on frame 0.
            self._mpv["pause"] = False
            self._paused = False
            self.btn_play.setText(_G_PAUSE)
            if not self.muted and not self._audio_started:
                self._enable_audio_track()
        except Exception as e:
            self._switching = False
            failed_context = (
                self._pending_native_context
                or self._native_active_context
                or (self._mpv_gen, self._track_generation,
                    item.get("Id"), url, session_id or self._emby_session_id)
            )
            logger.error("mpv loadfile failed: %s", e)
            self._sig_eof.emit(failed_context, "error")
            return False
        return True

    # ── gapless prefetch ──────────────────────────────────────────────────

    def prefetch(
        self,
        item: dict[str, Any],
        url: str,
        session_id: str,
        *,
        playback_plan: PlaybackPlan | None = None,
    ) -> bool:
        if not self._audio_arm_call_lock.acquire(blocking=False):
            return False
        try:
            return self._prefetch_impl(
                item, url, session_id, playback_plan=playback_plan,
            )
        finally:
            self._audio_arm_call_lock.release()

    def _prefetch_impl(
        self,
        item: dict[str, Any],
        url: str,
        session_id: str,
        *,
        playback_plan: PlaybackPlan | None = None,
    ) -> bool:
        """Queue the next item on the live mpv playlist.

        With prefetch-playlist=yes, mpv opens the queued entry's demuxer as
        soon as the current one is fully read (≈ demuxer_readahead_secs
        before EOF), so the network stream is already warm when we advance —
        probed at ~60ms to first frame vs a cold loadfile's open latency.
        """
        if self._closing or self._mpv is None:
            return False
        if self._prefetched is not None:
            if not self.drop_prefetch(audio_lock_held=True):
                return False
        try:
            self._mpv.command("loadfile", url, "append")
        except Exception as e:
            logger.debug("prefetch append failed: %s", e)
            return False
        self._prefetched = (item, url, session_id)
        self._prefetched_stream_url = url
        self._prefetched_playback_plan = playback_plan
        self._prefetch_fault_suppression_until = 0.0
        return True

    def _remove_prefetched_playlist_entry(
        self, *, audio_lock_held: bool = False,
    ) -> bool:
        if not audio_lock_held:
            if not self._audio_arm_call_lock.acquire(blocking=False):
                return False
            try:
                return self._remove_prefetched_playlist_entry(audio_lock_held=True)
            finally:
                self._audio_arm_call_lock.release()
        if self._mpv is None:
            return True
        try:
            current = getattr(self._mpv, "playlist_pos")
            count = getattr(self._mpv, "playlist_count")
            if current is None or count is None:
                return True
            next_index = int(current) + 1
            if int(count) > next_index:
                self._mpv.command("playlist-remove", next_index)
            return True
        except Exception as e:
            logger.debug("Prefetch playlist removal failed: %s", e)
            return False

    def _forget_prefetch_after_native_clear(self, *, requeue: bool = True) -> None:
        pending = self._prefetched
        self._prefetched = None
        self._prefetched_stream_url = None
        self._prefetched_playback_plan = None
        self._prefetch_request_token = None
        self._prefetch_drop_retry_count = 0
        if pending is None:
            return
        item, _url, session_id = pending
        if requeue:
            try:
                self.controller.playlists.push_front(
                    self.controller._cell_group(self), item,
                )
            except Exception as e:
                logger.debug("Prefetch item could not be requeued: %s", e)
        try:
            self.controller.stop_emby_session(item.get("Id"), session_id)
        except Exception as e:
            logger.debug("Prefetch session stop could not be queued: %s", e)

    def _schedule_prefetch_drop_retry(self, *, requeue: bool) -> None:
        if self._closing or self._prefetch_drop_retry_scheduled:
            return
        if self._prefetch_drop_retry_count >= 3:
            logger.error("Prefetch cleanup remains pending after bounded retries.")
            return
        token = self._current_playback_token()
        self._prefetch_drop_retry_count += 1
        self._prefetch_drop_retry_scheduled = True

        def _retry() -> None:
            self._prefetch_drop_retry_scheduled = False
            if (
                self._closing
                or token is None
                or not self._playback_token_is_current(token)
            ):
                return
            self.drop_prefetch(requeue=requeue)

        QTimer.singleShot(50, _retry)

    def drop_prefetch(
        self,
        *,
        audio_lock_held: bool = False,
        requeue: bool = True,
    ) -> bool:
        """Drop a queued resource transactionally."""
        if self._prefetch_advance_inflight is not None:
            self._invalidate_prefetched_advance()
        if self._prefetched is None:
            return True
        removed = self._remove_prefetched_playlist_entry(
            audio_lock_held=audio_lock_held,
        )
        if not removed:
            self._schedule_prefetch_drop_retry(requeue=requeue)
            return False
        self._forget_prefetch_after_native_clear(requeue=requeue)
        return True

    def _invalidate_prefetched_advance(self) -> None:
        """Invalidate a queued macOS playlist transition."""
        if self._prefetch_advance_inflight is None:
            return
        self._prefetch_advance_serial += 1
        self._prefetch_advance_inflight = None
        self._prefetch_advance_token = None
        self._prefetch_advance_pending = None
        self._pending_native_context = None
        self._switching = False

    def _prefetched_advance_is_current(
        self,
        request_id: int,
        mpv_ref: Any,
        token: PlaybackToken,
        pending: tuple[dict[str, Any], str, str],
    ) -> bool:
        return (
            not self._closing
            and self._prefetch_advance_inflight == request_id
            and self._mpv is mpv_ref
            and self._prefetch_advance_token == token
            and self._prefetch_advance_pending == pending
            and self._current_playback_token() == token
            and self._prefetched == pending
        )

    def _queue_prefetched_advance(self) -> bool:
        """Run macOS playlist advance off the Qt GUI thread."""
        if self._closing or self._prefetched is None or self._mpv is None:
            return False
        if self._prefetch_advance_inflight is not None:
            return True
        token = self._current_playback_token()
        pending = self._prefetched
        if token is None or pending is None:
            return False
        item, url, sid = pending
        self._cancel_audio_arm(timeout_s=0.0)
        with self._audio_arm_lock:
            self._prefetch_advance_serial += 1
            request_id = self._prefetch_advance_serial
            self._prefetch_advance_inflight = request_id
            self._prefetch_advance_token = token
            self._prefetch_advance_pending = pending
        self._switching = True
        self._track_done = False
        self._pending_native_context = (
            self._mpv_gen,
            self._track_generation + 1,
            item.get("Id"),
            url,
            sid,
        )
        self._playback_controller.transition(
            PlaybackEvent.ADVANCE_REQUESTED,
            self._current_playback_state_identity(),
        )
        self._playback_controller.transition(
            PlaybackEvent.LOAD_REQUESTED,
            self._playback_state_identity(self._pending_native_context),
        )
        mpv_ref = self._mpv
        worker = threading.Thread(
            target=self._prefetched_advance_worker,
            args=(request_id, mpv_ref, token, pending),
            name="mpv-prefetched-advance",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as e:
            logger.warning("Prefetched advance worker could not start: %s", e)
            self._invalidate_prefetched_advance()
            return False
        return True

    def _prefetched_advance_worker(
        self,
        request_id: int,
        mpv_ref: Any,
        token: PlaybackToken,
        pending: tuple[dict[str, Any], str, str],
    ) -> None:
        succeeded = False
        try:
            # The ownership lock covers the validity check and every native
            # operation. A cancellation token without the lock leaves a TOCTOU
            # window between checking the mpv handle and playlist-next.
            with self._audio_arm_call_lock:
                if not self._prefetched_advance_is_current(
                    request_id, mpv_ref, token, pending
                ):
                    return
                if STATS_ENABLED:
                    self._flush_stats(audio_lock_held=True)
                if self.muted and self._audio_started:
                    mpv_ref["aid"] = "no"
                mpv_ref.command("playlist-next")
                mpv_ref["pause"] = False
                succeeded = True
        except Exception as e:
            logger.warning("Prefetched advance failed (%s) — falling back to reload.", e)
        finally:
            try:
                self._sig_prefetched_advance.emit(request_id, succeeded)
            except Exception:
                # Native worker callbacks must not escape into teardown.
                pass

    def _finish_prefetched_advance(
        self, request_id: int, succeeded: bool,
    ) -> None:
        """Commit or cold-reload an off-thread prefetched transition."""
        if self._prefetch_advance_inflight != request_id:
            return
        token = self._prefetch_advance_token
        pending = self._prefetch_advance_pending
        mpv_ref = self._mpv
        self._prefetch_advance_inflight = None
        self._prefetch_advance_token = None
        self._prefetch_advance_pending = None
        if (
            token is None
            or pending is None
            or mpv_ref is None
            or self._closing
            or self._current_playback_token() != token
            or self._prefetched != pending
        ):
            return
        item, url, sid = pending
        prefetched_plan = self._prefetched_playback_plan
        if not succeeded:
            self._switching = False
            self._pending_native_context = None
            self._forget_prefetch_after_native_clear(requeue=False)
            logger.warning(
                "Prefetched advance failed — cold-loading the same item."
            )
            self.controller._hand_off(self, item)
            self.controller.sync_broadcast_cell_update(self)
            return

        old_item_id = self._emby_item_id
        old_session_id = self._emby_session_id
        start_seen = (
            self._native_active_context is not None
            and self._native_active_context[0] == self._mpv_gen
            and self._native_active_context[1] == self._track_generation + 1
            and self._native_active_context[2:4] == (item.get("Id"), url)
        )
        self._prefetched = None
        self._prefetched_stream_url = None
        self._prefetched_playback_plan = None
        self._prefetch_request_token = None
        self._prefetch_drop_retry_count = 0
        self._stream_url = url
        self._begin_track(item)
        if prefetched_plan is not None:
            self._playback_plan = prefetched_plan
        context: NativeContext = (
            self._mpv_gen, self._track_generation, item.get("Id"), url, sid,
        )
        self._native_active_context = context
        self._pending_native_context = None if start_seen else context
        self._switching = not start_seen
        if self.muted and self._audio_started:
            self._audio_started = False
        self._paused = False
        self.btn_play.setText(_G_PAUSE)
        if not self.muted and not self._audio_started:
            self._enable_audio_track()
        self._emby_session_id = sid
        self._emby_item_id = item["Id"]
        if old_item_id and old_session_id and old_session_id != sid:
            self.controller.stop_emby_session(old_item_id, old_session_id)
        self.controller._arm_prefetch(self)
        self.controller.sync_broadcast_cell_update(self)

    @traced("cell.advance_to_prefetched")
    def advance_to_prefetched(self) -> bool:
        if self._closing or self._prefetched is None or self._mpv is None:
            return False
        if sys.platform == "darwin":
            return self._queue_prefetched_advance()
        if not self._audio_arm_call_lock.acquire(blocking=False):
            return False
        try:
            self._cancel_audio_arm(timeout_s=0.0)
            return self._advance_to_prefetched_impl()
        finally:
            self._audio_arm_call_lock.release()

    def _advance_to_prefetched_impl(self) -> bool:
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
        prefetched_plan = self._prefetched_playback_plan
        old_item_id = self._emby_item_id
        old_session_id = self._emby_session_id
        if STATS_ENABLED:
            self._flush_stats(audio_lock_held=True)
        if self.muted and self._audio_started:
            try:
                self._mpv["aid"] = "no"
                self._audio_started = False
            except Exception as e:
                logger.debug("mpv: failed to re-disable aid: %s", e)
        self._switching = True
        self._track_done = False
        self._pending_native_context = (
            self._mpv_gen,
            self._track_generation + 1,
            item.get("Id"),
            _url,
            sid,
        )
        self._playback_controller.transition(
            PlaybackEvent.ADVANCE_REQUESTED,
            self._current_playback_state_identity(),
        )
        self._playback_controller.transition(
            PlaybackEvent.LOAD_REQUESTED,
            self._playback_state_identity(self._pending_native_context),
        )
        try:
            self._mpv.command("playlist-next")
            self._mpv["pause"] = False
            self._paused = False
            self.btn_play.setText(_G_PAUSE)
        except Exception as e:
            self._switching = False
            self._pending_native_context = None
            logger.warning(
                "Prefetched advance failed (%s) — falling back to reload.", e
            )
            return False
        self._prefetched = None
        self._stream_url = _url
        self._prefetched_stream_url = None
        self._prefetched_playback_plan = None
        self._prefetch_request_token = None
        self._begin_track(item)
        if prefetched_plan is not None:
            self._playback_plan = prefetched_plan
        if not self.muted and not self._audio_started:
            self._enable_audio_track()
        self._emby_session_id = sid
        self._emby_item_id = item["Id"]
        if old_item_id and old_session_id and old_session_id != sid:
            self.controller.stop_emby_session(old_item_id, old_session_id)
        self.controller._arm_prefetch(self)
        self.controller.sync_broadcast_cell_update(self)
        return True

    def _stop_qt_timers(self) -> None:
        """Stop all Qt-owned timers/animations on the widget's own thread."""
        for timer in (
            self._watchdog_timer,
            self._autohide_timer,
            self._ui_timer,
            self._overlay_show_timer,
        ):
            try:
                timer.stop()
            except Exception:
                pass
        for animation in (
            self._loading_pulse,
            self._overlay_anim,
            self._ctrl_anim,
        ):
            try:
                animation.stop()
            except Exception:
                pass

    def prepare_shutdown(self) -> None:
        """Quiesce GUI-owned state before mpv is released off-thread."""
        self._closing = True
        self._playback_controller.transition(
            PlaybackEvent.SHUTDOWN,
            self._current_playback_state_identity(),
        )
        self._invalidate_async_play()
        self._pending_next = False
        self._pending_next_token = None
        pending_play = self._deferred_play
        self._deferred_play = None
        if pending_play is not None and pending_play[3] is not None:
            pending_play[3](False)
        self._invalidate_prefetched_advance()
        self.drop_prefetch(requeue=False)
        self._cancel_audio_arm(timeout_s=0.0)
        self._stop_qt_timers()

    def release(
        self, *, wait_s: float | None = None,
        shutdown_deadline: float | None = None,
    ) -> None:
        """Release mpv after GUI-owned state has been quiesced."""
        if QThread.currentThread() is self.thread() and not self._closing:
            self.prepare_shutdown()
        if wait_s is None:
            wait_s = (
                max(0.0, shutdown_deadline - _time.monotonic())
                if shutdown_deadline is not None else 1.5
            )
        self._destroy_mpv(
            wait_s=wait_s, shutdown_deadline=shutdown_deadline,
        )

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

    def _pause_for_seek_native(self) -> None:
        if not self._dragging or self._closing:
            return
        self._native_call(
            lambda mpv: mpv.__setitem__("pause", True),
            retry=self._pause_for_seek_native,
        )

    @traced("cell._seek_press")
    def _seek_press(self) -> None:
        self._dragging = True
        self._autohide_timer.stop()
        # Remember the pre-drag pause state: releasing a seek used to
        # unconditionally resume, silently un-pausing a deliberately
        # paused cell (2026-07-13 audit).
        self._paused_before_seek = self._paused
        # _native_call serializes this write with _audio_arm_call_lock.
        self._paused = True
        self._native_call(
            lambda mpv: mpv.__setitem__("pause", True),
            retry=self._pause_for_seek_native,
        )

    @traced("cell._seek_release")
    def _seek_release(self, token: PlaybackToken | None = None) -> None:
        if self._closing:
            return
        if token is None:
            token = self._current_playback_token()
            if token is None:
                self._dragging = False
                return
        if not self._playback_token_is_current(token):
            self._dragging = False
            return
        if not self._audio_arm_call_lock.acquire(blocking=False):
            # The audio-arm worker owns the native handle. Let it finish its
            # aid/seek pair, then retry the user's seek instead of allowing a
            # stale cached-position seek to overwrite current UI intent.
            QTimer.singleShot(50, lambda token=token: self._seek_release(token))
            return
        try:
            # Invalidate any worker that has not entered the native section
            # yet. The ownership lock prevents one that is already inside from
            # racing this handler.
            self._cancel_audio_arm(timeout_s=0.0)
            if (
                token is not None
                and not self._playback_token_is_current(token)
            ):
                self._dragging = False
                return
            if self._mpv is not None and self._duration_s > 0:
                try:
                    # 0.98, not 0.90: the property-driven EOF advance makes
                    # the clip tail safe to seek into; 10% of every video was
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
        finally:
            self._audio_arm_call_lock.release()

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

    def _set_pause_from_controller(self, paused: bool) -> None:
        if self._closing or self._mpv is None:
            return
        self._paused = paused
        self.set_paused_ui(paused)
        self._native_call(
            lambda mpv: mpv.__setitem__("pause", paused),
            retry=lambda: self._set_pause_from_controller(paused),
        )

    def _run_native_commands(self, *commands: tuple[Any, ...]) -> bool:
        if self._closing or self._mpv is None:
            return False

        def _run(mpv: Any) -> None:
            for command in commands:
                mpv.command(*command)

        return self._native_call(
            _run,
            retry=lambda: self._run_native_commands(*commands),
        )

    @traced("cell._toggle_play")
    def _toggle_play(self) -> None:
        if self._mpv is None:
            return

        def _toggle(mpv: Any) -> None:
            new_pause = not bool(mpv["pause"])
            mpv["pause"] = new_pause
            self._paused = new_pause
            self.btn_play.setText(_G_PLAY if new_pause else _G_PAUSE)
            self._nudge_pill()

        self._native_call(_toggle, retry=self._toggle_play)

    def _toggle_loop(self) -> None:
        self.looping = self.btn_loop.isChecked()
        self._nudge_pill()
        if self._mpv is not None:
            value = "inf" if self.looping else "no"
            self._native_call(
                lambda mpv: mpv.__setitem__("loop-file", value),
                retry=self._toggle_loop,
            )

    def _audio_arm_is_current(
        self, token: int, mpv_ref: Any, track_generation: int,
    ) -> bool:
        """Return whether an audio-arm worker may still touch its mpv."""
        with self._audio_arm_lock:
            return (
                not self._closing
                and self._audio_arm_inflight_token == token
                and self._audio_arm_token == token
                and self._mpv is mpv_ref
                and self._track_generation == track_generation
            )

    def _cancel_audio_arm(self, timeout_s: float = 1.0) -> bool:
        """Invalidate and, briefly, drain a pending audio-arm worker.

        A worker that is already inside libmpv is allowed to finish its
        current call, but it is invalidated before a replacement load or
        teardown so its keyframe seek cannot target a different track. The
        boolean reports whether the worker actually drained; callers that
        replace a track must recreate mpv when it did not.
        """
        with self._audio_arm_lock:
            self._audio_arm_token += 1
            self._native_control_serial += 1
            self._native_control_tokens.clear()
            self._audio_arm_pending_enabled = None
            inflight = self._audio_arm_inflight_token
            done = self._audio_arm_done
        if inflight is None or done.wait(timeout_s):
            return True
        logger.warning(
            "Audio arm still running after %.1fs — abandoning worker.",
            timeout_s,
        )
        return False

    def _request_audio_track_state(self, enabled: bool) -> None:
        """Converge the native audio track on the latest mute request.

        ``mute=true`` only silences the audio output; it does not stop mpv's
        audio demux/decode work. Keep one serialized, token-bound transition
        for both arm and disarm so a rapid mute/unmute cannot let an old
        ``aid=auto`` completion resurrect hidden audio on the cell.
        """
        if self._closing or self._mpv is None:
            return
        if sys.platform != "darwin":
            token = self._current_playback_token()
            if enabled:
                self._enable_audio_track_sync(token)
            else:
                self._disable_audio_track_sync(token)
            return

        mpv_ref = self._mpv
        with self._audio_arm_lock:
            self._audio_arm_token += 1
            token = self._audio_arm_token
            self._audio_arm_pending_enabled = enabled
            if not enabled:
                # Reflect the requested state immediately; the native write is
                # still serialized below and may be waiting behind libmpv IPC.
                self._audio_started = False
            if self._audio_arm_inflight_token is not None:
                return
            self._audio_arm_pending_enabled = None
            self._audio_arm_inflight_token = token
            self._audio_arm_done.clear()
            track_generation = self._track_generation
            position = self._play_pos if self._play_pos > 0 else None
        worker = threading.Thread(
            target=self._audio_arm_worker,
            args=(token, mpv_ref, track_generation, position, enabled),
            name="mpv-audio-arm",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as e:
            with self._audio_arm_lock:
                if self._audio_arm_inflight_token == token:
                    self._audio_arm_inflight_token = None
                    self._audio_arm_done.set()
                self._audio_arm_pending_enabled = None
            logger.warning("Audio track transition could not start: %s", e)

    def _start_audio_arm(self) -> None:
        """Submit the potentially blocking aid/seek sequence to a daemon."""
        if self._closing:
            return
        self._request_audio_track_state(True)

    def _audio_arm_worker(
        self,
        token: int,
        mpv_ref: Any,
        track_generation: int,
        position: float | None,
        enabled: bool = True,
    ) -> None:
        """Run the latest lazy audio arm/disarm transition off Qt."""
        aid_ms = 0.0
        seek_ms = 0.0
        restart_enabled: bool | None = None
        try:
            # The lock covers the validity check and every native call. A
            # cancellation token alone leaves a TOCTOU window between the
            # final check and seek; transition/teardown paths take this lock
            # before replacing or freeing the mpv handle.
            with self._audio_arm_call_lock:
                if not self._audio_arm_is_current(
                    token, mpv_ref, track_generation
                ):
                    return
                started = _time.perf_counter()
                mpv_ref["aid"] = audio_track_for_mute(not enabled)
                aid_ms = (_time.perf_counter() - started) * 1000
                if enabled:
                    if not self._audio_arm_is_current(
                        token, mpv_ref, track_generation
                    ):
                        return
                    if position is not None:
                        started = _time.perf_counter()
                        mpv_ref.seek(position, "absolute+keyframes")
                        seek_ms = (_time.perf_counter() - started) * 1000
                if not self._audio_arm_is_current(
                    token, mpv_ref, track_generation
                ):
                    return
                with self._audio_arm_lock:
                    if (
                        not self._closing
                        and self._audio_arm_inflight_token == token
                    ):
                        self._audio_started = enabled
            if enabled:
                logger.info(
                    "AUDIO arm: aid=%.0fms seek=%.0fms cached-pos=%s",
                    aid_ms, seek_ms, "yes" if position is not None else "no",
                )
            else:
                logger.info("AUDIO disarm: aid=%.0fms", aid_ms)
        except Exception as e:
            logger.warning("Audio track transition failed: %s", e)
        finally:
            with self._audio_arm_lock:
                if self._audio_arm_inflight_token == token:
                    self._audio_arm_inflight_token = None
                    self._audio_arm_done.set()
                if (
                    self._audio_arm_pending_enabled is not None
                    and not self._closing
                    and self._mpv is mpv_ref
                    and self._track_generation == track_generation
                ):
                    restart_enabled = self._audio_arm_pending_enabled
                    self._audio_arm_pending_enabled = None
            if restart_enabled is not None:
                self._request_audio_track_state(restart_enabled)

    def _disable_audio_track(self) -> None:
        """Stop hidden audio demux/decode when a cell is muted."""
        if self._mpv is None:
            return
        with self._audio_arm_lock:
            active = (
                self._audio_started
                or self._audio_arm_inflight_token is not None
            )
        if active:
            self._request_audio_track_state(False)

    def _queue_mute_native(self, muted: bool) -> None:
        self._queue_native_property("mute", muted)

    def _native_control_is_current(
        self,
        name: str,
        token: int,
        mpv_ref: Any,
        track_generation: int,
    ) -> bool:
        with self._audio_arm_lock:
            return (
                not self._closing
                and self._native_control_tokens.get(name) == token
                and self._mpv is mpv_ref
                and self._track_generation == track_generation
            )

    def _queue_native_property(self, name: str, value: Any) -> None:
        """Write a potentially blocking mpv property without blocking Qt."""
        mpv_ref = self._mpv
        if mpv_ref is None or self._closing:
            return
        with self._audio_arm_lock:
            self._native_control_serial += 1
            token = self._native_control_serial
            self._native_control_tokens[name] = token
            track_generation = self._track_generation
        worker = threading.Thread(
            target=self._native_property_worker,
            args=(name, value, token, mpv_ref, track_generation),
            name=f"mpv-{name}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as e:
            logger.debug("Native %s write could not start: %s", name, e)

    def _native_property_worker(
        self,
        name: str,
        value: Any,
        token: int,
        mpv_ref: Any,
        track_generation: int,
    ) -> None:
        try:
            with self._audio_arm_call_lock:
                if not self._native_control_is_current(
                    name, token, mpv_ref, track_generation
                ):
                    return
                mpv_ref[name] = value
        except Exception as e:
            logger.debug("Native %s write failed: %s", name, e)

    def _enable_audio_track_sync(
        self, token: PlaybackToken | None = None,
    ) -> None:
        if self._closing:
            return
        if token is None:
            token = self._current_playback_token()
            if token is None:
                return
        if not self._playback_token_is_current(token):
            return
        if not self._audio_arm_call_lock.acquire(blocking=False):
            QTimer.singleShot(
                50, lambda token=token: self._enable_audio_track_sync(token),
            )
            return
        try:
            self._enable_audio_track_sync_locked(token)
        finally:
            self._audio_arm_call_lock.release()

    def _disable_audio_track_sync(
        self, token: PlaybackToken | None = None,
    ) -> None:
        """Stop the audio demuxer synchronously on non-macOS platforms."""
        if self._closing or self._mpv is None:
            return
        if token is None:
            token = self._current_playback_token()
            if token is None:
                return
        if not self._playback_token_is_current(token):
            return
        if not self._audio_arm_call_lock.acquire(blocking=False):
            QTimer.singleShot(
                50, lambda token=token: self._disable_audio_track_sync(token),
            )
            return
        try:
            if self._playback_token_is_current(token):
                self._mpv["aid"] = audio_track_for_mute(True)
                self._audio_started = False
                logger.info("AUDIO disarm: aid=0ms")
        except Exception as e:
            logger.warning("Audio track disarm failed on mute: %s", e)
        finally:
            self._audio_arm_call_lock.release()

    def _enable_audio_track_sync_locked(
        self, token: PlaybackToken | None = None,
    ) -> None:
        """Arm audio synchronously on platforms without the macOS GUI stall."""
        if (
            self._closing
            or self._audio_started
            or self._mpv is None
            or (token is not None and not self._playback_token_is_current(token))
        ):
            return
        try:
            t0 = _time.perf_counter()
            self._mpv["aid"] = "auto"
            aid_ms = (_time.perf_counter() - t0) * 1000
            self._audio_started = True
            # time-pos is maintained by the observer on the mpv event thread.
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
            logger.warning("Audio track arm failed on unmute: %s", e)

    def _enable_audio_track(self) -> None:
        """Start lazy audio arm without blocking the macOS Qt GUI thread.

        Muted cells load with aid=no (see _ensure_mpv). macOS uses the worker
        because its render-path soak showed long libmpv IPC stalls; Windows
        and Linux retain the established synchronous behavior.
        """
        if sys.platform != "darwin":
            self._enable_audio_track_sync(self._current_playback_token())
            return
        if self._audio_started or self._mpv is None:
            return
        try:
            self._start_audio_arm()
        except Exception as e:
            logger.warning("Audio track arm could not start: %s", e)

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

    def _write_native_property_latest(self, name: str, value: Any) -> None:
        mpv_ref = self._mpv
        if mpv_ref is None or self._closing:
            return
        with self._audio_arm_lock:
            self._native_control_serial += 1
            token = self._native_control_serial
            self._native_control_tokens[name] = token
            track_generation = self._track_generation

        def _attempt() -> None:
            if self._closing:
                return
            if not self._audio_arm_call_lock.acquire(blocking=False):
                QTimer.singleShot(50, _attempt)
                return
            try:
                if self._native_control_is_current(
                    name, token, mpv_ref, track_generation,
                ):
                    mpv_ref[name] = value
            except Exception as e:
                logger.debug("Native %s write failed: %s", name, e)
            finally:
                self._audio_arm_call_lock.release()

        _attempt()

    def _write_mute_native(self, muted: bool) -> None:
        self._write_native_property_latest("mute", muted)

    def _apply_mute(self, muted: bool) -> None:
        """Single writer for the mute state itself (cache + mpv + UI).

        Unmuting arms the audio track first (lazy — see _enable_audio_track),
        while muting disarms the track so silent cells do not keep decoding
        audio in the background.
        """
        self.muted = muted
        if muted:
            self._disable_audio_track()
        else:
            self._enable_audio_track()
        if self._mpv is not None:
            if sys.platform == "darwin":
                self._queue_mute_native(muted)
            else:
                self._write_mute_native(muted)
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

    def _write_volume_native(self, value: float) -> None:
        self._write_native_property_latest("volume", value)

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
            if sys.platform == "darwin":
                self._queue_native_property("volume", float(val))
            else:
                self._write_volume_native(float(val))
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

    def _hardware_decode_enabled(self) -> bool:
        if self._force_software_decode:
            return False
        opts = self._mpv_opts or apply_env_overrides(MPV_OPTS)
        value = str(opts.get("hwdec", "")).strip().lower()
        return value not in {"", "no", "none", "software", "false", "0"}

    def _current_playback_token(self) -> PlaybackToken | None:
        if self.current_item is None or self._stream_url is None:
            return None
        return PlaybackToken(
            self._mpv_gen,
            self._track_generation,
            self.current_item.get("Id"),
            self._stream_url,
        )

    @staticmethod
    def _playback_state_identity(
        context: NativeContext | None,
    ) -> PlaybackIdentity | None:
        if context is None:
            return None
        return PlaybackIdentity(
            context[0], context[1], context[2], context[3], context[4],
        )

    def _current_playback_state_identity(self) -> PlaybackIdentity | None:
        context = self._native_active_context or self._pending_native_context
        if context is not None:
            return self._playback_state_identity(context)
        return PlaybackIdentity(
            self._mpv_gen,
            self._track_generation,
            (self.current_item or {}).get("Id"),
            self._stream_url,
            self._emby_session_id,
        )

    def _playback_token_is_current(self, token: PlaybackToken) -> bool:
        return playback_token_is_current(
            token,
            mpv_generation=self._mpv_gen,
            track_generation=self._track_generation,
            item_id=(self.current_item or {}).get("Id"),
            stream_url=self._stream_url,
            closing=self._closing,
        )

    def _handle_prefetch_fault(
        self, context: NativeContext, _message: str,
    ) -> None:
        """Quarantine a malformed queued resource before activation."""
        pending = self._prefetched
        if (
            self._closing
            or self._mpv is None
            or pending is None
            or self._prefetch_advance_inflight is not None
        ):
            return
        item, url, sid = pending
        expected = (
            self._mpv_gen, self._track_generation + 1,
            item.get("Id"), url, sid,
        )
        if context != expected:
            return
        logger.error(
            "Malformed prefetched resource — quarantining before activation: %s",
            item.get("Name", "?"),
        )
        self._prefetch_fault_suppression_until = _time.monotonic() + 5.0
        self.resource_quarantined.emit(item)
        self.drop_prefetch(requeue=False)


    def _handle_decoder_fault(
        self, context: NativeContext, _message: str,
    ) -> None:
        """Recover one cell from a decoder/backend fault on the GUI thread."""
        if (
            self._closing
            or self._mpv is None
            or not self._native_context_is_current(context)
            or self._prefetch_advance_inflight is not None
        ):
            return
        token = self._current_playback_token()
        if token is None or self._decoder_recovery_scheduled:
            return
        if self._resource_quarantined:
            return
        self._playback_controller.transition(
            PlaybackEvent.RECOVERY_REQUESTED,
            self._playback_state_identity(context),
        )
        self._decoder_fault_count += 1
        plan = decoder_recovery_plan(
            self._decoder_fault_count,
            hardware_decode=self._hardware_decode_enabled(),
            max_faults=DECODER_FAULT_MAX,
            malformed_stream=is_malformed_stream_fault(_message),
        )
        if plan["action"] == "skip":
            logger.error(
                "Decoder recovery exhausted on cell — quarantining current resource."
            )
            with self._stats_lock:
                self._decoder_recovery_exhausted += 1
                self._decoder_quarantines += 1
            self._resource_quarantined = True
            self._track_done = True
            self._notify_resource_quarantined()
            self._request_next_throttled(False)
            return
        if plan["action"] == "fallback-software":
            with self._stats_lock:
                self._decoder_software_fallbacks += 1
            self._force_software_decode = True
            if self._playback_plan is not None:
                self._playback_plan = self._playback_plan.with_client_decoder("no")
            logger.warning(
                "Decoder fault on cell — recreating with software decode for item."
            )
        else:
            logger.warning("Software decoder fault on cell — recreating demuxer.")
        self._decoder_recovery_scheduled = True
        self._decoder_recovery_token = token
        QTimer.singleShot(
            0, lambda token=token: self._recover_current_decoder(token),
        )

    def _recover_current_decoder(self, token: PlaybackToken) -> None:
        if self._decoder_recovery_token == token:
            self._decoder_recovery_scheduled = False
            self._decoder_recovery_token = None
        if not self._playback_token_is_current(token):
            return
        self.play(
            self.current_item, token.stream_url,
            preserve_failure_state=True,
        )

    def _handle_transport_fault(
        self, context: NativeContext, _message: str,
    ) -> None:
        """Retry a failed resource once, then advance without escalation."""
        if (
            self._closing
            or self._mpv is None
            or not self._native_context_is_current(context)
            or self._prefetch_advance_inflight is not None
        ):
            return
        if self._resource_quarantined or self._transport_recovery_scheduled:
            return
        self._playback_controller.transition(
            PlaybackEvent.RECOVERY_REQUESTED,
            self._playback_state_identity(context),
        )
        token = self._current_playback_token()
        if token is None:
            return
        self._transport_retry_count += 1
        plan = transport_recovery_plan(
            self._transport_retry_count, max_attempts=TRANSPORT_RETRY_MAX,
        )
        if plan["action"] == "retry":
            self.drop_prefetch()
            self._transport_recovery_scheduled = True
            self._transport_recovery_token = token
            logger.warning(
                "Transport fault on cell — retrying current resource once."
            )
            delay_ms = int(float(str(plan["delay_s"])) * 1000)
            QTimer.singleShot(
                delay_ms,
                lambda token=token: self._retry_transport_resource(token),
            )
            return
        logger.error(
            "Transport recovery exhausted on cell — skipping current resource."
        )
        self._transport_resource_quarantined = True
        self._resource_quarantined = True
        self._track_done = True
        self._request_next_throttled(False)

    def _retry_transport_resource(self, token: PlaybackToken) -> None:
        if self._transport_recovery_token == token:
            self._transport_recovery_scheduled = False
            self._transport_recovery_token = None
        if not self._playback_token_is_current(token):
            return
        self._request_next_throttled(True, token=token)


    def _handle_buffering(
        self, context: NativeContext, buffering: bool,
    ) -> None:
        """GUI-thread side of the paused-for-cache observer.

        Turns invisible network-starvation freezes into: a pulsing
        BUFFERING card on the cell, a WARNING log with the measured
        duration, and per-cell counters that ride the stats dump.
        """
        if (
            self._closing
            or self._mpv is None
            or not self._native_context_is_current(context)
        ):
            return
        self._playback_controller.transition(
            PlaybackEvent.BUFFERING_STARTED if buffering else PlaybackEvent.BUFFERING_ENDED,
            self._playback_state_identity(context),
        )
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
                state = self._cache_buffering_state
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
                if tag == "cache starvation":
                    self._starvation_track_events += 1
                    self._starvation_track_total_s += dur
                    if (
                        not self._starvation_fault_scheduled
                        and not self._track_done
                        and starvation_fault_reached(
                            self._starvation_track_events,
                            self._starvation_track_total_s,
                            max_events=STARVATION_FAULT_EVENTS,
                            max_total_s=STARVATION_FAULT_TOTAL_S,
                        )
                    ):
                        self._starvation_fault_scheduled = True
                        logger.error(
                            "Starvation fault on '%s': %d episodes / %.0fs "
                            "cumulative — quarantining resource and advancing.",
                            (self.current_item or {}).get("Name", "?"),
                            self._starvation_track_events,
                            self._starvation_track_total_s,
                        )
                        self._resource_quarantined = True
                        self._track_done = True
                        self._notify_resource_quarantined()
                        self._request_next_throttled(False)
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

    def _loop_current_track(self, token: PlaybackToken) -> None:
        if not self._playback_token_is_current(token):
            return

        def _loop(mpv: Any) -> None:
            mpv.seek(0, "absolute")
            mpv["pause"] = False
            self._paused = False

        self._native_call(
            _loop,
            retry=lambda: self._loop_current_track(token),
            valid=lambda: self._playback_token_is_current(token),
        )

    @traced("cell._handle_track_done")
    def _handle_track_done(self, context: NativeContext) -> None:
        """A track finished naturally (eof-reached flipped True).

        This is the wall's primary advance path: with keep_open="always" mpv
        emits NO end-file at natural EOF — it pauses on the last frame and
        flips the eof-reached property. The signal is queued from the mpv
        event thread, so re-check liveness against the CURRENT player state:
        a stale signal from a track that play() has since replaced must not
        advance the new one.
        """
        if (
            self._closing
            or self._mpv is None
            or not self._native_context_is_current(context)
        ):
            return
        if self._paused:
            # Explicitly paused (global pause / user) — don't yank the wall
            # forward underneath a pause. The resume path re-checks
            # eof-reached and advances then (wall._global_toggle_pause).
            return
        if self._switching or self._track_done:
            return
        if not self._eof_reached:
            return  # stale — the track this signal was about is gone
        if not self._played_anything:
            logger.warning("Track ended before first frame — treating as error.")
            self._track_done = True
            self._on_error()
            return
        if self.looping:
            token = self._current_playback_token()
            if token is not None:
                self._loop_current_track(token)
            return
        self._track_done = True
        logger.info(
            "Track finished: %s — advancing.",
            (self.current_item or {}).get("Name", "?"),
        )
        self._request_next_throttled(False)

    def _handle_eof(
        self, context: NativeContext, reason: str,
    ) -> None:
        if self._closing or context[0] != self._mpv_gen:
            return
        if context[1] != self._track_generation:
            if reason in ("stop", "quit", "redirect", "restarted"):
                self._switching = False
            return
        if not self._native_context_is_current(context):
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
            if self._pending_native_context == context:
                self._pending_native_context = None
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
            token = self._current_playback_token()
            if token is not None:
                self._loop_current_track(token)
        else:
            self._track_done = True
            self._request_next_throttled(False)

    def _notify_resource_quarantined(self) -> None:
        """Tell the wall a resource is quarantined so it can skip re-picks.

        The wall keeps a session-scoped set of quarantined item IDs and
        filters them out of every draw; per-cell flags alone would let a
        quarantined resource be re-picked and re-freeze moments later.
        """
        item = self.current_item or {}
        if item.get("Id"):
            self.resource_quarantined.emit(item)

    def _request_next_throttled(
        self,
        is_retry: bool,
        *,
        token: PlaybackToken | None = None,
    ) -> None:
        if self._closing or (token is not None and not self._playback_token_is_current(token)):
            return
        MIN_INTERVAL = 0.75
        now = _time.monotonic()
        elapsed = now - self._last_next_request_ts
        if not is_retry and elapsed < MIN_INTERVAL:
            # Defer instead of dropping: a dropped EOF advance used to leave
            # the cell frozen on its last frame until the stall watchdog
            # rescued it 20s later. One pending advance at a time.
            if not self._pending_next:
                token = self._current_playback_token()
                if token is None:
                    return
                self._pending_next = True
                self._pending_next_token = token
                delay_ms = int((MIN_INTERVAL - elapsed) * 1000) + 50
                logger.warning(
                    "next_video throttled (last fire %.2fs ago) — "
                    "deferring %dms", elapsed, delay_ms,
                )
                QTimer.singleShot(
                    delay_ms,
                    lambda token=token: self._fire_pending_next(token),
                )
            return
        self._pending_next = False
        self._pending_next_token = None
        self._last_next_request_ts = now
        if not is_retry:
            self._playback_controller.transition(
                PlaybackEvent.ADVANCE_REQUESTED,
                self._current_playback_state_identity(),
            )
        self.request_next.emit(self, is_retry)

    def _fire_pending_next(self, token: PlaybackToken) -> None:
        if (
            self._closing
            or not self._pending_next
            or self._pending_next_token != token
            or not self._playback_token_is_current(token)
        ):
            return
        self._pending_next = False
        self._pending_next_token = None
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
        if (
            self._closing
            or self._mpv is None
            or self._parked
            or self._prefetch_advance_inflight is not None
        ):
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
            token = self._current_playback_token()
            self._park_token = token
            QTimer.singleShot(
                CRASH_LOOP_COOLDOWN_S * 1000,
                lambda token=token: self._unpark(token),
            )
            return True
        return False

    def _unpark(self, token: PlaybackToken | None = None) -> None:
        """Leave the parked state after the cooldown and try to resume."""
        if token is not None and self._park_token != token:
            return
        if self._closing or not self._parked:
            return
        if token is not None and not self._playback_token_is_current(token):
            return
        self._park_token = None
        self._parked = False
        self._failure_ts.clear()
        self._retry_count = 0
        self._force_transcode = False
        logger.info("Crash-loop cooldown elapsed — resuming cell.")
        self._request_next_throttled(False)

    def _on_error(self) -> None:
        if (
            self._closing
            or self._parked
            or self._transport_recovery_scheduled
            or self._resource_quarantined
        ):
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
        token = self._current_playback_token()
        if token is None:
            return
        self._playback_controller.transition(
            PlaybackEvent.RECOVERY_REQUESTED,
            self._current_playback_state_identity(),
        )
        self._retry_backoff_token = token
        self._retry_count += 1
        if outage:
            outage_plan = outage_recovery_plan(
                self._retry_count, MAX_RETRIES,
            )
            if outage_plan["action"] == "park":
                self._parked = True
                self._park_token = token
                logger.error(
                    "Systemic outage retry budget exhausted — parking cell "
                    "for %ds.", CRASH_LOOP_COOLDOWN_S,
                )
                self._show_title_overlay(
                    "Media unavailable — retrying soon…", sticky=True,
                )
                QTimer.singleShot(
                    CRASH_LOOP_COOLDOWN_S * 1000,
                    lambda token=token: self._unpark(token),
                )
                return
            logger.warning(
                "Playback error (attempt %d/%d)", self._retry_count, MAX_RETRIES
            )
            delay_s = apply_jitter(OUTAGE_BACKOFF_S, random.random())
            logger.warning(
                "Systemic outage suspected — backing off %.1fs without "
                "transcode escalation.", delay_s,
            )
            QTimer.singleShot(
                int(delay_s * 1000),
                lambda token=token: self._request_next_throttled(
                    True, token=token,
                ),
            )
            return
        logger.warning(
            "Playback error (attempt %d/%d)", self._retry_count, MAX_RETRIES
        )
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
                lambda token=token: self._request_next_throttled(
                    True, token=token,
                ),
            )
        else:
            logger.error("Max retries reached — skipping.")
            self._force_transcode = False
            self._request_next_throttled(False)

    # ── input handling ──────────────────────────────────────────────────────────────────

    def mouseDoubleClickEvent(self, event: Any) -> None:
        """Double-click a cell to toggle full-screen solo in its window.

        Ctrl+double-click requests a remote solo on other synced displays.
        """
        if event.button() == Qt.MouseButton.LeftButton:
            if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
                self.request_remote_solo.emit(self)
            else:
                self.request_solo.emit(self)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
