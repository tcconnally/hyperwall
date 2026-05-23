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

# ── GPU telemetry helper ────────────────────────────────────────────────────
def _query_gpu_telemetry() -> dict | None:
    """Query NVIDIA GPU VRAM, utilisation, decoder load, temp via nvidia-smi.

    Returns a dict on success, None if nvidia-smi is unavailable or fails.
    Called at stats dump time so it never blocks the Qt event loop.
    """
    import subprocess
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,utilization.gpu,utilization.decoder,temperature.gpu,pstate",
                "--format=csv,noheader,nounits",
            ],
            text=True, timeout=3,
            creationflags=0x08000000 if os.name == "nt" else 0,  # CREATE_NO_WINDOW
        )
        parts = [p.strip() for p in out.strip().split(",")]
        if len(parts) >= 6:
            return {
                "memory_used":   parts[0],
                "memory_total":  parts[1],
                "gpu_util":      parts[2],
                "decoder_util":  parts[3],
                "temp":          parts[4],
                "pstate":        parts[5],
            }
    except Exception as e:
        logger.debug("GPU telemetry unavailable: %s", e)
    return None

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
    def __init__(self, settings: dict, api):
        self.settings   = settings
        self.api        = api
        self.cells:    list[VideoCell]   = []
        self.windows:  list[QMainWindow] = []
        self._shortcuts: list[QShortcut] = []
        self.all_items: list[dict] = []
        self.filtered:  list[dict] = []
        self.playlist:  deque[dict] = deque()
        self.controls_visible = True
        self._api_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="api")
        self._api_pool_closed = False
        self._cleaned_up = False
        self._shutdown_requested = False
        self._escape_filter = _EmergencyKeyFilter(self._shutdown)
        QApplication.instance().installEventFilter(self._escape_filter)

        self._build_displays()
        # Show all windows at once to avoid ghost flashes from sequential
        # creation of native video-frame handles.
        for win in self.windows:
            win.showFullScreen()
            logger.info("Display active: %s", win.windowTitle())
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
                ("F",      lambda: self._set_filter("favorites")),
                ("A",      lambda: self._set_filter("all")),
                ("S",      self._toggle_stats_overlay),
                ("Escape", self._shutdown),
            ):
                shortcut = QShortcut(QKeySequence(key), win)
                shortcut.activated.connect(fn)
                self._shortcuts.append(shortcut)

            win.setGeometry(screen.geometry())
            self.windows.append(win)
            logger.info("Display built: %s", screen.name())

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
        from .emby import classify_item
        iid  = item["Id"]
        key  = self.api.access_token
        base = self.api.server_url
        sid  = uuid.uuid4().hex

        classification = classify_item(item)
        # 'immediate' forces transcode on first load; 'auto' does the
        # resolution/bitrate/subtitle gate.  force_transcode (retry escalation)
        # overrides everything.
        auto_transcode = classification != "direct" or force_transcode
        if force_transcode or classification == "immediate":
            url = (f"{base}/Videos/{iid}/master.m3u8?api_key={key}"
                   f"&VideoCodec=h264&AudioCodec=aac&MaxAudioChannels=2"
                   f"&MaxHeight=1080&MaxWidth=1920"
                   f"&MaxFramerate=30&VideoBitrate=12000000"
                   f"&PlaySessionId={sid}")
            tag = "TRANSCODE/immediate" if classification == "immediate" else "TRANSCODE/retry"
            logger.info("[%s] %s", tag, item.get("Name"))
        elif classification == "auto":
            url = (f"{base}/Videos/{iid}/master.m3u8?api_key={key}"
                   f"&VideoCodec=h264&AudioCodec=aac&MaxAudioChannels=2"
                   f"&MaxHeight=1080&MaxWidth=1920"
                   f"&MaxFramerate=30&VideoBitrate=12000000"
                   f"&PlaySessionId={sid}")
            logger.info("[TRANSCODE/auto] %s", item.get("Name"))
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
        self._submit_api(_worker, "stop-session")

    def _submit_api(self, fn, label: str):
        if self._api_pool_closed:
            logger.debug("API task skipped after shutdown: %s", label)
            return None
        try:
            return self._api_pool.submit(fn)
        except RuntimeError as e:
            logger.debug("API task rejected during shutdown (%s): %s", label, e)
            return None

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

    def _global_toggle_pause(self):
        active_mpvs = [c for c in self.cells if c._mpv is not None]
        if not active_mpvs: return
        try:
            any_playing = any(not bool(c._mpv["pause"]) for c in active_mpvs)
        except Exception as e:
            logger.debug("Pause state read failed, assuming paused: %s", e)
            any_playing = False
        for c in active_mpvs:
            try:
                c._mpv["pause"] = any_playing
                c.btn_play.setText("▶" if any_playing else "⏸")
            except Exception as e:
                logger.debug("Pause toggle failed on cell: %s", e)

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
        self._submit_api(_worker, "update-tags")

    def update_favorite(self, item_id: str, state: bool):
        def _worker():
            try:
                path = f"/Users/{self.api.user_id}/FavoriteItems/{item_id}"
                (self.api.post if state else self.api.delete)(path, timeout=7)
                logger.info("API: Favorite toggled for %s → %s", item_id, state)
            except Exception as e:
                logger.error("API: Favorite error: %s", e)
        self._submit_api(_worker, "update-favorite")

    def _shutdown(self):
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        logger.info("Shutdown requested.")
        self._cleanup()
        QApplication.instance().quit()

    def _cleanup(self):
        if self._cleaned_up:
            return
        self._cleaned_up = True

        # Hide all windows immediately so the user never sees a black/stuck frame.
        for w in self.windows:
            try:
                w.hide()
            except Exception:
                pass

        for c in self.cells:
            self.stop_emby_session(c._emby_item_id, c._emby_session_id)
        if STATS_ENABLED:
            for c in self.cells:
                try: c._flush_stats()
                except Exception as e:
                    logger.warning("stats flush failed: %s", e)
        for c in self.cells:
            try:
                c.release()
            except Exception as e:
                logger.warning("Cell release failed: %s", e)
        if STATS_ENABLED:
            self._dump_stats_json()
        # Signal no new submissions, then drain in-flight API calls (session-stop
        # requests fired above) with a short bounded wait before tearing down.
        # Use a daemon thread to enforce the timeout — shutdown(wait=True) has no
        # built-in deadline, and a hung network call would stall Qt's quit loop.
        self._api_pool_closed = True
        import threading as _threading
        _drain = _threading.Thread(
            target=self._api_pool.shutdown, kwargs={"wait": True}, daemon=True
        )
        _drain.start()
        _drain.join(timeout=6.0)  # slightly longer than the longest API timeout (5 s)
        if _drain.is_alive():
            logger.warning("API pool drain timed out — forcing shutdown.")
        try:
            QApplication.instance().removeEventFilter(self._escape_filter)
        except Exception as e:
            logger.debug("removeEventFilter failed: %s", e)
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
        gpu_snapshot = _query_gpu_telemetry()
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "n_cells": len(self.cells),
            "mpv_opts_effective": apply_perf_env(MPV_OPTS),
            "env": {k: os.environ.get(k) for k in (
                "HYPERWALL_STATS", "HYPERWALL_HDR_HINT", "HYPERWALL_HWDEC",
                "HYPERWALL_GPU_API", "HYPERWALL_PROFILE", "HYPERWALL_VIDEO_SYNC",
            ) if os.environ.get(k) is not None},
            "gpu": gpu_snapshot,
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
        if gpu_snapshot:
            g = gpu_snapshot
            logger.info(
                "STATS GPU  mem=%s/%s  util=%s%%  dec=%s%%  temp=%s°C  pstate=%s",
                g.get("memory_used"), g.get("memory_total"),
                g.get("gpu_util"), g.get("decoder_util"),
                g.get("temp"), g.get("pstate"),
            )
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
