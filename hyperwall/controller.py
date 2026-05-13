import logging
import os
import random
import threading
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import (
    Qt, QObject, QEvent, QTimer, pyqtSignal, pyqtSlot
)
from PyQt6.QtGui import QShortcut, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout, QLabel
)

from .perf import (
    logger, STREAM_START_STAGGER_MS, STATS_ENABLED, apply_perf_env,
    MPV_OPTS, SCRIPT_DIR, API_POOL_MAX_WORKERS,
    TRANSCODE_VIDEO_CODEC, TRANSCODE_AUDIO_CODEC, TRANSCODE_AUDIO_CHANNELS,
    TRANSCODE_MAX_HEIGHT, TRANSCODE_MAX_WIDTH, TRANSCODE_MAX_FRAMERATE,
    TRANSCODE_VIDEO_BITRATE
)
from .cell import VideoCell
from .emby import ContentLoaderThread, EMBY_API_TIMEOUT_MEDIUM, EMBY_API_TIMEOUT_LONG

# Optional companion module
try:
    from hyperwall_remix import remix_walls as _remix_walls
except ImportError:
    _remix_walls = None


class _EmergencyKeyFilter(QObject):
    """App-level last-resort key handler for shortcuts stolen by child widgets.

    Normal wall shortcuts stay registered per fullscreen window because that is
    the proven multi-monitor Qt focus model for HyperWall. This filter is only
    an additive safety net for Escape so exiting the wall never depends on which
    cell/control/native mpv child currently owns focus.
    """

    def __init__(self, shutdown_callback):
        super().__init__()
        self._shutdown_callback = shutdown_callback

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self._shutdown_callback()
            return True
        return False


class WallController:
    """Manages the overall HyperWall application lifecycle, display grid,
    content loading, and user interactions.

    Orchestrates multiple VideoCell instances across connected monitors,
    handles global shortcuts, and communicates with the Emby API.
    """
    def __init__(self, settings: dict, api):
        """Initializes the WallController.

        Args:
            settings (dict): Configuration settings for the wall (e.g., screens, libraries, grid layout).
            api (EmbyAPISession): An authenticated Emby API session instance.
        """
        super().__init__()
        self.settings   = settings
        self.api        = api
        self.cells:    list[VideoCell]   = []
        self.windows:  list[QMainWindow] = []
        self._shortcuts: list[QShortcut] = []
        self.all_items: list[dict] = []
        self.filtered:  list[dict] = []
        self.playlist:  deque[dict] = deque()
        self.controls_visible = True
        self._api_pool = ThreadPoolExecutor(max_workers=API_POOL_MAX_WORKERS, thread_name_prefix="api")
        self._api_pool_closed = False
        self._cleaned_up = False
        self._shutdown_requested = False
        self._escape_filter = _EmergencyKeyFilter(self._shutdown)
        QApplication.instance().installEventFilter(self._escape_filter)

        self._build_displays()
        self._start_async_load()

    def _build_displays(self):
        """Sets up the QMainWindow instances for each selected screen and arranges
        VideoCell widgets in a grid layout within them.

        Also registers global keyboard shortcuts for wall control.
        """
        rows, cols = self.settings["grid"]
        for screen in self.settings["screens"]:
            win = QMainWindow()
            win.setWindowTitle(f"HyperWall — {screen.name()}")
            win.setStyleSheet("background: black;")

            cw = QWidget(); win.setCentralWidget(cw)
            grid = QGridLayout(cw); grid.setContentsMargins(0, 0, 0, 0); grid.setSpacing(0)

            for r in range(rows):
                for c in range(cols):
                    cell = VideoCell(self)
                    cell.request_next.connect(self.next_video)
                    cell.request_prev.connect(self.prev_video)
                    grid.addWidget(cell, r, c)
                    self.cells.append(cell)

            for key, fn in (
                ("C",      self._global_toggle_controls),
                ("Space",  self._global_toggle_pause),
                ("F",      lambda: self._set_filter("favorites")),
                ("A",      lambda: self._set_filter("all")),
                ("S",      self._toggle_stats_overlay),
                ("R",      self._open_remix_dialog),
                ("Escape", self._shutdown),
            ):
                shortcut = QShortcut(QKeySequence(key), win)
                shortcut.activated.connect(fn)
                self._shortcuts.append(shortcut)

            win.setGeometry(screen.geometry())
            win.showFullScreen()
            self.windows.append(win)
            logger.info("Display active: %s", screen.name())

    def _start_async_load(self):
        """Initiates asynchronous loading of media content from Emby using a dedicated thread."""
        self.loader = ContentLoaderThread(self.api, self.settings["libraries"])
        self.loader.finished.connect(self._on_items_loaded)
        self.loader.start()

    def _on_items_loaded(self, items: list[dict]):
        """Callback executed after media items have been loaded from Emby.

        Initializes the content playlist and starts playback in each video cell.

        Args:
            items (list[dict]): A list of Emby media item dictionaries.
        """
        self.all_items = items
        self.filtered  = items[:]
        logger.info("Metadata Index: %d items loaded.", len(items))
        if not items:
            logger.warning("No items returned — check config.ini libraries.")
            for cell in self.cells:
                lbl = QLabel("No items found—check config.ini libraries", cell)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setStyleSheet(
                    "color: #666; font-size: 13px; font-family: 'Segoe UI';"
                    " background: transparent;"
                )
                lbl.resize(cell.video_frame.size()); lbl.show()
            return
        for i, cell in enumerate(self.cells):
            QTimer.singleShot(i * STREAM_START_STAGGER_MS,
                              lambda c=cell: self.next_video(c, False))

    def _build_url(self, item: dict, force_transcode: bool = False) -> tuple[str, str]:
        """Constructs the appropriate Emby stream URL for a given media item.

        Decides between direct streaming and transcoding based on item properties
        and the `force_transcode` flag.

        Args:
            item (dict): The Emby media item dictionary.
            force_transcode (bool): If True, forces server-side transcoding.

        Returns:
            tuple[str, str]: A tuple containing the stream URL and the generated
                             PlaySessionId.
        """
        from .emby import needs_transcode
        iid  = item["Id"]
        key  = self.api.access_token
        base = self.api.server_url
        sid  = uuid.uuid4().hex

        auto_transcode = needs_transcode(item)
        if force_transcode or auto_transcode:
            # These parameters are hardcoded for 1080p H264 AAC stereo output.
            # For more flexibility, these could be moved to perf.py or config.ini.
            url = (f"{base}/Videos/{iid}/master.m3u8?api_key={key}"
                   f"&VideoCodec={TRANSCODE_VIDEO_CODEC}&AudioCodec={TRANSCODE_AUDIO_CODEC}&MaxAudioChannels={TRANSCODE_AUDIO_CHANNELS}"
                   f"&MaxHeight={TRANSCODE_MAX_HEIGHT}&MaxWidth={TRANSCODE_MAX_WIDTH}"
                   f"&MaxFramerate={TRANSCODE_MAX_FRAMERATE}&VideoBitrate={TRANSCODE_VIDEO_BITRATE}"
                   f"&PlaySessionId={sid}")
            tag = "TRANSCODE/retry" if force_transcode else "TRANSCODE/auto"
            logger.info("[%s] %s", tag, item.get("Name"))
        else:
            url = f"{base}/Videos/{iid}/stream?api_key={key}&static=true"
            logger.info("[DIRECT] %s", item.get("Name"))
        return url, sid

    def stop_emby_session(self, item_id: str | None, session_id: str | None):
        """Notifies the Emby server that a playback session has stopped.

        Args:
            item_id (str | None): The ID of the media item that was playing.
            session_id (str | None): The PlaySessionId of the stopped session.
        """
        if not item_id or not session_id:
            return
        def _worker():
            try:
                r = self.api.post("/Sessions/Playing/Stopped",
                                  json={"ItemId": item_id,
                                        "PlaySessionId": session_id,
                                        "PositionTicks": 0},
                                  timeout=EMBY_API_TIMEOUT_MEDIUM)
                logger.info("Session stop %s -> HTTP %d", session_id[:8], r.status_code)
            except requests.exceptions.RequestException:
                logger.exception("Stop-session %s failed", session_id[:8])
            except Exception:
                logger.exception("Unexpected error stopping Emby session %s", session_id[:8])
        self._submit_api(_worker, "stop-session")

    def _submit_api(self, fn, label: str):
        """Submits an Emby API-related task to the API thread pool for asynchronous execution.

        Args:
            fn (callable): The function to execute in the thread pool.
            label (str): A descriptive label for the API task for logging purposes.

        Returns:
            Future | None: A Future object representing the pending result, or None if
                           the API pool is closed.
        """
        if self._api_pool_closed:
            logger.debug("API task skipped after shutdown: %s", label)
            return None
        try:
            return self._api_pool.submit(fn)
        except RuntimeError:
            logger.exception("API task rejected during shutdown (%s)", label)
        except Exception:
            logger.exception("Unexpected error submitting API task (%s)", label)
        return None

    def _hand_off(self, cell: VideoCell, item: dict, force_transcode: bool = False):
        """Initiates playback of a given media item on a specific video cell.

        Stops any existing Emby session for the cell before starting new playback.

        Args:
            cell (VideoCell): The VideoCell instance to play the item on.
            item (dict): The Emby media item dictionary.
            force_transcode (bool): If True, forces server-side transcoding for this item.
        """
        self.stop_emby_session(cell._emby_item_id, cell._emby_session_id)
        url, sid = self._build_url(item, force_transcode)
        cell._emby_session_id = sid
        cell._emby_item_id    = item["Id"]
        cell.play(item, url)

    def next_video(self, cell: VideoCell, is_retry: bool = False):
        """Requests the next video in the playlist for a given cell.

        If the playlist is empty, it shuffles all filtered items and repopulates it.

        Args:
            cell (VideoCell): The VideoCell requesting the next video.
            is_retry (bool): True if this is a retry attempt after a playback error.
        """
        if not self.filtered: return
        if is_retry and cell.current_item:
            self._hand_off(cell, cell.current_item, cell._force_transcode)
            return
        if cell.current_item:
            cell.history.append(cell.current_item)
        if not self.playlist:
            shuffled = self.filtered[:]; random.shuffle(shuffled)
            self.playlist = deque(shuffled)
        item = self.playlist.pop()
        self._hand_off(cell, item)

    def prev_video(self, cell: VideoCell):
        """Requests the previous video from the cell's history.

        Args:
            cell (VideoCell): The VideoCell requesting the previous video.
        """
        if cell.history:
            item = cell.history.pop()
            self._hand_off(cell, item)

    def _global_toggle_controls(self):
        """Toggles the visibility of controls across all video cells."""
        self.controls_visible = not self.controls_visible
        for c in self.cells:
            c.set_controls_visible(self.controls_visible)
        logger.info("Controls: %s", "VISIBLE" if self.controls_visible else "HIDDEN")

    def _open_remix_dialog(self):
        """Opens the remix dialog if the `hyperwall_remix` module is available."""
        if _remix_walls is None:
            logger.warning("Remix unavailable: hyperwall_remix module missing.")
            return
        parent = self.windows[0] if self.windows else None
        try:
            _remix_walls(parent)
        except Exception:
            logger.exception("Remix dialog failed to launch")

    def _global_toggle_pause(self):
        """Toggles the global play/pause state for all active video cells."""
        active_mpvs = [c for c in self.cells if c._mpv is not None]
        if not active_mpvs: return
        try:
            any_playing = any(not bool(c._mpv["pause"]) for c in active_mpvs)
        except Exception:
            logger.exception("Error checking mpv pause status")
            any_playing = False # Assume not playing on error
        for c in active_mpvs:
            try:
                c._mpv["pause"] = any_playing
                c.btn_play.setText("▶" if any_playing else "⏸")
            except Exception:
                logger.exception("Error setting mpv pause state for cell")

    def _set_filter(self, mode: str):
        """Applies a filter to the media items (e.g., 'favorites' or 'all').

        Repopulates the playlist based on the selected filter.

        Args:
            mode (str): The filter mode ('favorites' or 'all').
        """
        if mode == "favorites":
            subset = [i for i in self.all_items if i.get("UserData", {}).get("IsFavorite")]
            if not subset:
                logger.warning("Filter: No favorites found."); return
            self.filtered = subset
        else:
            self.filtered = self.all_items[:]
        self.playlist.clear()
        logger.info("Filter: %s (%d items)", mode.upper(), len(self.filtered))
        for i, c in enumerate(self.cells):
            QTimer.singleShot(i * STREAM_START_STAGGER_MS,
                              lambda cell=c: self.next_video(cell, False))

    def update_tags(self, item: dict):
        """Updates the tags for a given Emby media item via the Emby API.

        Args:
            item (dict): The Emby media item with updated tags.
        """
        iid  = item["Id"]
        name = item.get("Name", "Unknown")
        raw  = item.get("Tags", [])
        tags = ([t.get("Name", "") for t in raw]
                if raw and isinstance(raw[0], dict) else list(raw))
        def _worker():
            try:
                data = self.api.get(f"/Users/{self.api.user_id}/Items/{iid}", timeout=EMBY_API_TIMEOUT_MEDIUM).json()
                data["Tags"] = tags
                # Remove server-generated fields that cause conflicts on PUT
                for k in ("ServerId", "Etag", "DateCreated", "CanDelete", "CanDownload",
                          "UserData", "Chapters", "ImageTags", "BackdropImageTags",
                          "TagItems", "ExternalUrls", "PlayAccess"):
                    data.pop(k, None)
                self.api.post(f"/Items/{iid}", json=data, timeout=EMBY_API_TIMEOUT_MEDIUM)
                logger.info("API: Tags updated for '%s'", name)
            except requests.exceptions.RequestException:
                logger.exception("API: Tag error for '%s'", name)
            except Exception:
                logger.exception("Unexpected error updating tags for '%s'", name)
        self._submit_api(_worker, "update-tags")

    def update_favorite(self, item_id: str, state: bool):
        """Updates the favorite status of a given Emby media item via the Emby API.

        Args:
            item_id (str): The ID of the media item.
            state (bool): True to mark as favorite, False to unfavorite.
        """
        def _worker():
            try:
                path = f"/Users/{self.api.user_id}/FavoriteItems/{item_id}"
                (self.api.post if state else self.api.delete)(path, timeout=EMBY_API_TIMEOUT_MEDIUM)
                logger.info("API: Favorite toggled for %s → %s", item_id, state)
            except requests.exceptions.RequestException:
                logger.exception("API: Favorite error for %s", item_id)
            except Exception:
                logger.exception("Unexpected error toggling favorite for %s", item_id)
        self._submit_api(_worker, "update-favorite")

    def _shutdown(self):
        """Initiates the shutdown sequence for the HyperWall application."""
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        logger.info("Shutdown requested.")
        self._cleanup()
        QApplication.instance().quit()

    def _cleanup(self):
        """Performs cleanup operations before application exit.

        Includes stopping Emby sessions, flushing statistics, and shutting down
        the API thread pool.
        """
        if self._cleaned_up:
            return
        self._cleaned_up = True
        for c in self.cells:
            self.stop_emby_session(c._emby_item_id, c._emby_session_id)
        if STATS_ENABLED:
            for c in self.cells:
                try: c._flush_stats()
                except Exception:
                    logger.exception("stats flush failed for cell")
        for c in self.cells:
            try: c.release()
            except Exception:
                logger.exception("Error releasing mpv cell")
        if STATS_ENABLED:
            self._dump_stats_json()
        self._api_pool_closed = True
        try:
            self._api_pool.shutdown(wait=False, cancel_futures=False)
        except TypeError:
            logger.exception("API pool shutdown failed due to TypeError (likely already shutdown)")
            self._api_pool.shutdown(wait=False)
        except Exception:
            logger.exception("Error shutting down API pool")
        try:
            QApplication.instance().removeEventFilter(self._escape_filter)
        except Exception:
            logger.exception("Error removing event filter")
        self.api.close()
        logger.info("Cleanup complete.")

    def _toggle_stats_overlay(self):
        """Toggles the mpv statistics overlay on the first video cell."""
        if not self.cells:
            return
        cell = self.cells[0]
        if cell._mpv is None:
            logger.info("Stats: cell 0 has no live mpv yet.")
            return
        try:
            cell._mpv.command("script-binding", "stats/display-stats-toggle")
            cell._mpv.command("script-binding", "stats/display-page-2")
            logger.info("Stats overlay toggled on cell 0 (page 2).")
        except Exception:
            logger.exception("Stats overlay toggle failed")

    def _dump_stats_json(self):
        """Dumps collected playback statistics to a JSON file for analysis."""
        import json, time
        cells_payload = []
        for i, c in enumerate(self.cells):
            cells_payload.append({
                "cell": i,
                "totals": dict(c._stats_total),
                "info":   {k: v for k, v in c._stats_info.items()},
                "last_item": (c.current_item or {}).get("Name"),
            })
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "n_cells": len(self.cells),
            "mpv_opts_effective": apply_perf_env(MPV_OPTS),
            "env": {k: os.environ.get(k) for k in (
                "HYPERWALL_STATS", "HYPERWALL_HDR_HINT", "HYPERWALL_HWDEC",
                "HYPERWALL_GPU_API", "HYPERWALL_PROFILE", "HYPERWALL_VIDEO_SYNC",
            ) if os.environ.get(k) is not None},
            "cells": cells_payload,
        }
        out = os.path.join(SCRIPT_DIR, f"hyperwall_stats_{int(time.time())}.json")
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            logger.info("STATS dump: %s", out)
        except Exception:
            logger.exception("STATS dump failed")
            return
        for s in cells_payload:
            t = s["totals"]
            i = s["info"]
            logger.info(
                "STATS cell %d  drop=%g  mistimed=%g  vo-delayed=%g  dec-drop=%g"
                "  hwdec=%s  fps=%s  bitrate=%s",
                s["cell"],
                t.get("frame-drop-count", 0),
                t.get("mistimed-frame-count", 0),
                t.get("vo-delayed-frame-count", 0),
                t.get("decoder-frame-drop-count", 0),
                i.get("hwdec-current"),
                i.get("estimated-vf-fps") or i.get("container-fps"),
                i.get("video-bitrate"),
            )

class MouseIdleHider(QObject):
    """Automatically hides the mouse cursor after a period of inactivity and restores it on movement."""
    def __init__(self, idle_ms):
        """Initializes the MouseIdleHider.

        Args:
            idle_ms (int): The idle time in milliseconds before the cursor is hidden.
        """
        super().__init__()
        self._hidden = False
        self._timer = QTimer(); self._timer.setSingleShot(True)
        self._timer.setInterval(idle_ms)
        self._timer.timeout.connect(self._hide)
        QApplication.instance().installEventFilter(self)
        self._timer.start()

    def eventFilter(self, obj, event):
        """Filters mouse move events to detect inactivity and toggle cursor visibility.

        Args:
            obj (QObject): The object for which the event was generated.
            event (QEvent): The event that occurred.

        Returns:
            bool: True if the event was handled, False otherwise.
        """
        if event.type() == QEvent.Type.MouseMove:
            if self._hidden:
                QApplication.restoreOverrideCursor()
                self._hidden = False
            self._timer.start()
        return False

    def _hide(self):
        """Hides the mouse cursor."""
        if not self._hidden:
            QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)
            self._hidden = True
