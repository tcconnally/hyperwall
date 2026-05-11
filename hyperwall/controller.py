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
    MPV_OPTS, SCRIPT_DIR
)
from .cell import VideoCell
from .emby import ContentLoaderThread

# Optional companion module
try:
    from hyperwall_remix import remix_walls as _remix_walls
except ImportError:
    _remix_walls = None

class WallController:
    def __init__(self, settings: dict, api):
        self.settings   = settings
        self.api        = api
        self.cells:    list[VideoCell]   = []
        self.windows:  list[QMainWindow] = []
        self.all_items: list[dict] = []
        self.filtered:  list[dict] = []
        self.playlist:  deque[dict] = deque()
        self.controls_visible = True
        self._api_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="api")

        self._build_displays()
        self._start_async_load()

    def _build_displays(self):
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
                ("M",      self._global_toggle_mute),
                ("F",      lambda: self._set_filter("favorites")),
                ("A",      lambda: self._set_filter("all")),
                ("S",      self._toggle_stats_overlay),
                ("R",      self._open_remix_dialog),
                ("Escape", self._shutdown),
            ):
                QShortcut(QKeySequence(key), win).activated.connect(fn)

            win.setGeometry(screen.geometry())
            win.showFullScreen()
            self.windows.append(win)
            logger.info("Display active: %s", screen.name())

    def _start_async_load(self):
        self.loader = ContentLoaderThread(self.api, self.settings["libraries"])
        self.loader.finished.connect(self._on_items_loaded)
        self.loader.start()

    def _on_items_loaded(self, items: list[dict]):
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
        from .emby import needs_transcode
        iid  = item["Id"]
        key  = self.api.access_token
        base = self.api.server_url
        sid  = uuid.uuid4().hex

        auto_transcode = needs_transcode(item)
        if force_transcode or auto_transcode:
            url = (f"{base}/Videos/{iid}/master.m3u8?api_key={key}"
                   f"&VideoCodec=h264&AudioCodec=aac&MaxAudioChannels=2"
                   f"&MaxHeight=1080&MaxWidth=1920"
                   f"&MaxFramerate=30&VideoBitrate=12000000"
                   f"&PlaySessionId={sid}")
            tag = "TRANSCODE/retry" if force_transcode else "TRANSCODE/auto"
            logger.info("[%s] %s", tag, item.get("Name"))
        else:
            url = f"{base}/Videos/{iid}/stream?api_key={key}&static=true"
            logger.info("[DIRECT] %s", item.get("Name"))
        return url, sid

    def stop_emby_session(self, item_id: str | None, session_id: str | None):
        if not item_id or not session_id:
            return
        def _worker():
            try:
                r = self.api.post("/Sessions/Playing/Stopped",
                                  json={"ItemId": item_id,
                                        "PlaySessionId": session_id,
                                        "PositionTicks": 0},
                                  timeout=5)
                logger.info("Session stop %s -> HTTP %d", session_id[:8], r.status_code)
            except Exception as e:
                logger.warning("Stop-session %s failed: %s", session_id[:8], e)
        self._api_pool.submit(_worker)

    def _hand_off(self, cell: VideoCell, item: dict, force_transcode: bool = False):
        self.stop_emby_session(cell._emby_item_id, cell._emby_session_id)
        url, sid = self._build_url(item, force_transcode)
        cell._emby_session_id = sid
        cell._emby_item_id    = item["Id"]
        cell.play(item, url)

    def next_video(self, cell: VideoCell, is_retry: bool = False):
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
        if cell.history:
            item = cell.history.pop()
            self._hand_off(cell, item)

    def _global_toggle_controls(self):
        self.controls_visible = not self.controls_visible
        for c in self.cells:
            c.set_controls_visible(self.controls_visible)
        logger.info("Controls: %s", "VISIBLE" if self.controls_visible else "HIDDEN")

    def _open_remix_dialog(self):
        if _remix_walls is None:
            logger.warning("Remix unavailable: hyperwall_remix module missing.")
            return
        parent = self.windows[0] if self.windows else None
        try:
            _remix_walls(parent)
        except Exception:
            logger.exception("Remix dialog failed to launch")

    def _global_toggle_pause(self):
        active_mpvs = [c for c in self.cells if c._mpv is not None]
        if not active_mpvs: return
        try:
            any_playing = any(not bool(c._mpv["pause"]) for c in active_mpvs)
        except Exception:
            any_playing = False
        for c in active_mpvs:
            try:
                c._mpv["pause"] = any_playing
                c.btn_play.setText("▶" if any_playing else "⏸")
            except Exception: pass

    def _global_toggle_mute(self):
        if not self.cells:
            return
        new_muted = not all(c.muted for c in self.cells)
        for c in self.cells:
            c.muted = new_muted
            c.btn_mute.setChecked(new_muted)
            c.btn_mute.setText("🔇" if new_muted else "🔊")
            if not new_muted and c.vol_slider.value() == 0:
                c.vol_slider.blockSignals(True)
                c.vol_slider.setValue(70)
                c.vol_slider.blockSignals(False)
            if c._mpv is not None:
                try:
                    c._mpv["mute"] = new_muted
                    if not new_muted and c.vol_slider.value() > 0:
                        c._mpv["volume"] = float(c.vol_slider.value())
                except Exception:
                    pass
        logger.info("Audio: %s", "MUTED" if new_muted else "UNMUTED")

    def _set_filter(self, mode: str):
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
        iid  = item["Id"]
        name = item.get("Name", "Unknown")
        raw  = item.get("Tags", [])
        tags = ([t.get("Name", "") for t in raw]
                if raw and isinstance(raw[0], dict) else list(raw))
        def _worker():
            try:
                data = self.api.get(f"/Users/{self.api.user_id}/Items/{iid}", timeout=7).json()
                data["Tags"] = tags
                for k in ("ServerId", "Etag", "DateCreated", "CanDelete", "CanDownload",
                          "UserData", "Chapters", "ImageTags", "BackdropImageTags",
                          "TagItems", "ExternalUrls", "PlayAccess"):
                    data.pop(k, None)
                self.api.post(f"/Items/{iid}", json=data, timeout=7)
                logger.info("API: Tags updated for '%s'", name)
            except Exception as e:
                logger.error("API: Tag error for '%s': %s", name, e)
        self._api_pool.submit(_worker)

    def update_favorite(self, item_id: str, state: bool):
        def _worker():
            try:
                path = f"/Users/{self.api.user_id}/FavoriteItems/{item_id}"
                (self.api.post if state else self.api.delete)(path, timeout=7)
                logger.info("API: Favorite toggled for %s → %s", item_id, state)
            except Exception as e:
                logger.error("API: Favorite error: %s", e)
        self._api_pool.submit(_worker)

    def _shutdown(self):
        logger.info("Shutdown requested.")
        QApplication.instance().quit()

    def _cleanup(self):
        for c in self.cells:
            self.stop_emby_session(c._emby_item_id, c._emby_session_id)
        if STATS_ENABLED:
            for c in self.cells:
                try: c._flush_stats()
                except Exception as e:
                    logger.warning("stats flush failed: %s", e)
        for c in self.cells:
            try: c.release()
            except Exception: pass
        if STATS_ENABLED:
            self._dump_stats_json()
        self.api.close()
        logger.info("Cleanup complete.")

    def _toggle_stats_overlay(self):
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
        except Exception as e:
            logger.warning("Stats overlay toggle failed (stats.lua not loaded?): %s", e)

    def _dump_stats_json(self):
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
        except Exception as e:
            logger.warning("STATS dump failed: %s", e)
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
    def __init__(self, idle_ms):
        super().__init__()
        self._hidden = False
        self._timer = QTimer(); self._timer.setSingleShot(True)
        self._timer.setInterval(idle_ms)
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
