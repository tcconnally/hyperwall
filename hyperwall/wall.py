"""
Hyperwall — WallController.

Manages the multi-monitor video wall: creates fullscreen windows per monitor,
populates them with VideoCell grids, handles keyboard shortcuts, filtering,
pause/resume, and shutdown.
"""

from __future__ import annotations

import json
import logging
import os
import time as _time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PyQt6.QtCore import (
    QEvent,
    QObject,
    Qt,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt6.QtGui import QShortcut, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QGridLayout,
    QWidget,
)

from .cell import VideoCell
from .perftrace import traced
from .constants import (
    MAX_DIRECT_FPS,
    MAX_CONCURRENT_TRANSCODES,
    effective_bitrate_budget_mbps,
    OUTAGE_MIN_CELLS,
    OUTAGE_WINDOW_S,
    STREAM_START_STAGGER_MS,
    STATS_ENABLED,
    STATS_COUNTER_PROPS,
    STATS_INFO_PROPS,
    apply_cache_budget,
    apply_env_overrides,
    MPV_OPTS,
    SCRIPT_DIR,
)
from .emby import EmbyClient, ContentLoader
from .urls import needs_transcode as _needs_transcode_pure
from .reliability import is_systemic_outage, gate_auto_transcode
from .urls import build_stream_url, tag_names
from .playlist import PlaylistManager, DEFAULT_GROUP

logger = logging.getLogger("HyperWall")


class EmergencyKeyFilter(QObject):
    """App-level escape handler — works even when mpv children steal focus."""

    def __init__(self, shutdown_callback: callable):
        super().__init__()
        self._shutdown_callback = shutdown_callback

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            self._shutdown_callback()
            return True
        return False


class MouseIdleHider(QObject):
    """Hides the mouse cursor after a period of inactivity."""

    def __init__(self, idle_ms: int):
        super().__init__()
        self._hidden = False
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(idle_ms)
        self._timer.timeout.connect(self._hide)
        QApplication.instance().installEventFilter(self)
        self._timer.start()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseMove:
            if self._hidden:
                QApplication.restoreOverrideCursor()
                self._hidden = False
            self._timer.start()
        return False

    def _hide(self) -> None:
        if not self._hidden:
            QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)
            self._hidden = True


class MainThreadInvoker(QObject):
    """Marshals callables onto the GUI thread via a queued signal.

    Qt widgets (and QTimer creation) are only safe on the thread that owns
    them; the Flask web remote runs on its own threads. Emitting `call` from
    any thread queues the callable into the main event loop.
    """

    call = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @pyqtSlot(object)
    def _run(self, fn: Any) -> None:
        try:
            fn()
        except Exception:
            logger.exception("Main-thread invocation failed")


class WallController:
    """Orchestrates the video wall across multiple monitors."""

    def __init__(
        self,
        screens: list[Any],        # QScreen objects
        libraries: list[str],
        grid_rows: int,
        grid_cols: int,
        client: EmbyClient,
    ):
        self.client = client
        self.screens = screens
        self.libraries = libraries
        self.grid_rows = grid_rows
        self.grid_cols = grid_cols

        self.cells: list[VideoCell] = []
        self.windows: list[QMainWindow] = []
        self._shortcuts: list[QShortcut] = []
        self.all_items: list[dict[str, Any]] = []
        self.filtered: list[dict[str, Any]] = []
        self.filter_mode = "all"  # explicit mode; avoids O(n) list compares
        # Per-source-group playout. Default: a single "all" group shared by
        # every cell → identical to the prior single global shuffled deque
        # (global de-dup until the pool is exhausted). Per-monitor sourcing
        # (Epic 4) assigns cells to different groups.
        self.playlists = PlaylistManager()
        # Must match the cells' initial hidden state: True here made the
        # first C-press a no-op and /api/controls report success without
        # acting (2026-07-13 audit).
        self.controls_visible = False

        # Thread management
        self._api_pool = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="api"
        )
        self._api_pool_closed = False
        self._cleaned_up = False
        self._shutdown_requested = False

        # Cross-thread → GUI-thread marshaling (used by the web remote).
        # Must be constructed on the main thread so queued calls land here.
        self._invoker = MainThreadInvoker()

        # Systemic-outage tracking: (monotonic ts, cell id) per playback
        # failure, consulted by cells to decide whether to escalate.
        self._failure_events: deque[tuple[float, int]] = deque(maxlen=512)
        self._last_outage_log_ts = 0.0

        # Emergency escape
        self._escape_filter = EmergencyKeyFilter(self._shutdown)
        QApplication.instance().installEventFilter(self._escape_filter)

        self._build_displays()

        # Memory-aware demuxer budget: now that every cell exists, scale the
        # per-cell demuxer cache so the grid total stays under CACHE_BUDGET_MB.
        n_cells = len(self.cells)
        budgeted = apply_cache_budget(apply_env_overrides(MPV_OPTS), n_cells)
        for cell in self.cells:
            cell._mpv_opts = budgeted
        # Burst-aware budgets (2026-07-14): 80% of freeze episodes began
        # within 8s of a stream-open — the wall was starving itself with
        # its own fill-bursts at 8 cells. Readahead depth is scaled inside
        # apply_cache_budget; the direct-play bitrate cap scales here.
        self._bitrate_budget_mbps = effective_bitrate_budget_mbps(n_cells)
        logger.info(
            "MPV cache budget: %d cells → demuxer_max_bytes=%s, "
            "readahead=%ss, direct-play bitrate cap=%s Mbps",
            n_cells, budgeted.get("demuxer_max_bytes"),
            budgeted.get("demuxer_readahead_secs"),
            self._bitrate_budget_mbps,
        )

        for win in self.windows:
            win.showFullScreen()
            logger.info("Display active: %s", win.windowTitle())

        self._start_async_load()

    # ── display construction ──────────────────────────────────────────────

    def _build_displays(self) -> None:
        rows, cols = self.grid_rows, self.grid_cols
        for screen in self.screens:
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

            # Keyboard shortcuts per window
            for key, fn in (
                ("C", self._global_toggle_controls),
                ("Space", self._global_toggle_pause),
                ("F", lambda: self._set_filter("favorites")),
                ("A", lambda: self._set_filter("all")),
                ("S", self._toggle_stats_overlay),
                ("Escape", self._shutdown),
            ):
                shortcut = QShortcut(QKeySequence(key), win)
                shortcut.activated.connect(fn)
                self._shortcuts.append(shortcut)

            win.setGeometry(screen.geometry())
            self.windows.append(win)
            logger.info("Display built: %s", screen.name())

    # ── content loading ───────────────────────────────────────────────────

    def _start_async_load(self) -> None:
        self.loader = ContentLoader(self.client, self.libraries)
        self.loader.finished.connect(self._on_items_loaded)
        self.loader.start()

    def _on_items_loaded(self, items: list[dict[str, Any]]) -> None:
        self.all_items = items
        self.filtered = items[:]
        self.playlists.set_source(self.filtered, DEFAULT_GROUP)
        logger.info("Metadata Index: %d items loaded.", len(items))
        if not items:
            logger.warning("No items returned — check config.ini libraries.")
            for cell in self.cells:
                # play() never runs for these cells: stop the endless
                # LOADING pulse explicitly, and raise the label — an
                # unraised Qt sibling can render BEHIND the native video
                # HWND (2026-07-13 audit).
                cell._hide_overlay()
                lbl = QLabel("No items found — check config.ini libraries", cell)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setStyleSheet(
                    "color: #666; font-size: 13px; font-family: 'Segoe UI';"
                    " background: transparent;"
                )
                lbl.resize(cell.video_frame.size())
                lbl.show()
                lbl.raise_()
            return
        for i, cell in enumerate(self.cells):
            QTimer.singleShot(
                i * STREAM_START_STAGGER_MS,
                lambda c=cell: self.next_video(c, False),
            )

    # ── URL construction ──────────────────────────────────────────────────

    def _build_url(
        self, item: dict[str, Any], force_transcode: bool = False,
        prefetch: bool = False, cell: "VideoCell | None" = None,
    ) -> tuple[str, str]:
        iid = item["Id"]
        key = self.client.access_token
        base = self.client.server_url
        sid = uuid.uuid4().hex

        auto_transcode = _needs_transcode_pure(
            item,
            auto_transcode=os.environ.get(
                "HYPERWALL_AUTO_TRANSCODE", "1") == "1",
            max_fps=MAX_DIRECT_FPS,
            max_bitrate_mbps=self._bitrate_budget_mbps,
        )
        # Concurrency gate: a forced retry (failed direct) must transcode, but
        # an AUTO escalation defers to direct-play when the transcode engine is
        # already busy — never stampede greg's media engine (2026-07-15).
        gated = False
        if force_transcode:
            transcode = True
        else:
            active = sum(
                1 for c in self.cells
                if c is not cell and getattr(c, "_is_transcoding", False)
            )
            transcode = gate_auto_transcode(
                auto_transcode, active, MAX_CONCURRENT_TRANSCODES,
            )
            gated = auto_transcode and not transcode
        url = build_stream_url(
            base=base, item_id=iid, api_key=key,
            session_id=sid, transcode=transcode,
            static=self.client.backend.requires_static_true,
        )
        if transcode:
            tag = "TRANSCODE/retry" if force_transcode else "TRANSCODE/auto"
        else:
            tag = "DIRECT/gated" if gated else "DIRECT"
        if prefetch:
            tag += "/prefetch"
        logger.info("[%s] %s", tag, item.get("Name"))
        return url, sid

    # ── session management ────────────────────────────────────────────────

    def stop_emby_session(
        self, item_id: str | None, session_id: str | None,
    ) -> None:
        if not item_id or not session_id:
            return

        def _worker() -> None:
            try:
                r = self.client.post(
                    "/Sessions/Playing/Stopped",
                    json={
                        "ItemId": item_id,
                        "PlaySessionId": session_id,
                        "PositionTicks": 0,
                    },
                    timeout=5,
                )
                logger.info(
                    "Session stop %s -> HTTP %d", session_id[:8], r.status_code
                )
            except Exception as e:
                logger.warning(
                    "Stop-session %s failed: %s", session_id[:8], e
                )

        self._submit_api(_worker, "stop-session")

    def _submit_api(self, fn: callable, label: str) -> Any:
        if self._api_pool_closed:
            logger.debug("API task skipped after shutdown: %s", label)
            return None
        try:
            return self._api_pool.submit(fn)
        except RuntimeError as e:
            logger.debug(
                "API task rejected during shutdown (%s): %s", label, e
            )
            return None

    # ── playout ───────────────────────────────────────────────────────────

    @traced("wall._hand_off")
    def _hand_off(
        self,
        cell: VideoCell,
        item: dict[str, Any],
        force_transcode: bool = False,
    ) -> None:
        # Don't stop old sessions mid-playback — the async API call races
        # with the new stream creation, and Emby can kill both when it sees
        # a session-stop from the same device. Sessions are cleaned up on
        # wall shutdown via _cleanup().
        url, sid = self._build_url(item, force_transcode, cell=cell)
        cell._emby_session_id = sid
        cell._emby_item_id = item["Id"]
        cell.play(item, url)
        self._arm_prefetch(cell)

    @traced("wall._arm_prefetch")
    def _arm_prefetch(self, cell: VideoCell) -> None:
        """Schedule a playlist warmup after the active GUI transition returns.

        ``loadfile append`` has to execute on the cell's mpv/GUI ownership
        path, but the M5 soak showed it can take 170–200ms.  Deferring one Qt
        turn keeps the visible next/mute handler short while preserving the
        existing in-order mpv playlist semantics.  The closure re-checks
        liveness and consumes its item only when it actually runs.
        """
        def _queue() -> None:
            if self._shutdown_requested or cell._mpv is None:
                return
            item = self.playlists.next(self._cell_group(cell))
            if item is None:
                return
            url, sid = self._build_url(item, prefetch=True, cell=cell)
            if not cell.prefetch(item, url, sid):
                logger.debug("Prefetch declined for %s.", item.get("Name", "?"))

        QTimer.singleShot(0, _queue)

    def run_on_main(self, fn: Any) -> None:
        """Queue a callable onto the GUI thread (safe from any thread)."""
        self._invoker.call.emit(fn)

    def register_failure(self, cell: VideoCell) -> bool:
        """Record a cell playback failure; True if it looks systemic.

        "Systemic" = a majority of the wall (min OUTAGE_MIN_CELLS distinct
        cells) failed within OUTAGE_WINDOW_S — a shared cause like Emby or
        the network, where per-cell transcode escalation only piles load
        onto an already-struggling server.
        """
        now = _time.monotonic()
        self._failure_events.append((now, id(cell)))
        outage = is_systemic_outage(
            self._failure_events, now,
            window_s=OUTAGE_WINDOW_S,
            total_cells=len(self.cells),
            min_cells=OUTAGE_MIN_CELLS,
        )
        if outage and now - self._last_outage_log_ts > 10.0:
            self._last_outage_log_ts = now
            logger.warning(
                "Systemic outage suspected: majority of cells failing within "
                "%ds — cells backing off without transcode escalation.",
                OUTAGE_WINDOW_S,
            )
        return outage

    def _cell_group(self, cell: VideoCell) -> str:
        """Source group a cell draws from. Defaults to the shared 'all' group;
        per-monitor sourcing sets cell._source_group to a distinct key."""
        return getattr(cell, "_source_group", DEFAULT_GROUP) or DEFAULT_GROUP

    @traced("wall.next_video")
    def next_video(self, cell: VideoCell, is_retry: bool = False) -> None:
        if is_retry and cell.current_item:
            self._hand_off(cell, cell.current_item, cell._force_transcode)
            return
        prev = cell.current_item
        # Fast path: the next item is already queued and warmed on this
        # cell's mpv playlist — the advance is a ~60ms cut, not a cold open.
        if cell.advance_to_prefetched():
            if prev:
                cell.history.append(prev)
            logger.info(
                "[PREFETCH→] %s", (cell.current_item or {}).get("Name"),
            )
            self._arm_prefetch(cell)
            return
        item = self.playlists.next(self._cell_group(cell))
        if item is None:
            return
        if prev:
            cell.history.append(prev)
        self._hand_off(cell, item)

    @traced("wall.prev_video")
    def prev_video(self, cell: VideoCell) -> None:
        if cell.history:
            item = cell.history.pop()
            self._hand_off(cell, item)

    # ── global controls ───────────────────────────────────────────────────

    def _global_toggle_controls(self) -> None:
        self.controls_visible = not self.controls_visible
        for c in self.cells:
            c.set_controls_visible(self.controls_visible)
        logger.info(
            "Controls: %s", "VISIBLE" if self.controls_visible else "HIDDEN"
        )

    @traced("wall._global_toggle_pause")
    def _global_toggle_pause(self) -> None:
        active_mpvs = [c for c in self.cells if c._mpv is not None]
        if not active_mpvs:
            return
        # Per-cell reads: one wedged mpv used to abort the whole generator
        # and force any_playing=False — turning a global PAUSE press into a
        # global RESUME (2026-07-13 audit). Unreadable cells count as paused.
        states = []
        for c in active_mpvs:
            try:
                states.append(not bool(c._mpv["pause"]))
            except Exception as e:
                logger.warning("Pause state read failed on a cell: %s", e)
                states.append(False)
        any_playing = any(states)
        for c in active_mpvs:
            try:
                c._mpv["pause"] = any_playing
                c._paused = any_playing
                c.set_paused_ui(any_playing)
            except Exception as e:
                logger.debug("Pause toggle failed on cell: %s", e)
        if not any_playing:
            # RESUME: cells that hit natural EOF while paused skipped their
            # advance (paused cells don't auto-advance); re-arm them now.
            for c in active_mpvs:
                try:
                    if c._mpv.eof_reached is True:
                        self.next_video(c, False)
                except Exception:
                    pass

    @traced("wall._set_filter")
    def _set_filter(self, mode: str) -> None:
        if mode == "favorites":
            subset = [
                i for i in self.all_items
                if i.get("UserData", {}).get("IsFavorite")
            ]
            if not subset:
                logger.warning("Filter: No favorites found.")
                return
            self.filtered = subset
        else:
            self.filtered = self.all_items[:]
        self.filter_mode = mode
        self.playlists.set_source(self.filtered, DEFAULT_GROUP)
        logger.info("Filter: %s (%d items)", mode.upper(), len(self.filtered))
        # Queued prefetches were drawn from the OLD pool — drop them so the
        # restart below can't fast-path into an item the filter excludes.
        for c in self.cells:
            c.drop_prefetch()
        for i, c in enumerate(self.cells):
            QTimer.singleShot(
                i * STREAM_START_STAGGER_MS,
                lambda cell=c: self.next_video(cell, False),
            )

    # ── tag / favorite mutations ──────────────────────────────────────────

    def update_tags(self, item: dict[str, Any]) -> None:
        iid = item["Id"]
        name = item.get("Name", "Unknown")
        # Read via the helper (Emby serves tags under TagItems, Tags is null).
        tags = tag_names(item)

        def _worker() -> None:
            try:
                data = self.client.get(
                    f"/Users/{self.client.user_id}/Items/{iid}", timeout=7
                ).json()
                data["Tags"] = tags
                for k in (
                    "ServerId", "Etag", "DateCreated", "CanDelete",
                    "CanDownload", "UserData", "Chapters", "ImageTags",
                    "BackdropImageTags", "TagItems", "ExternalUrls",
                    "PlayAccess",
                ):
                    data.pop(k, None)
                r = self.client.post(f"/Items/{iid}", json=data, timeout=7)
                if r.status_code >= 300:
                    # Non-2xx used to be logged as success while the server
                    # rejected the write (2026-07-13 audit).
                    logger.error(
                        "API: Tag update REJECTED for '%s' (HTTP %d) — "
                        "the flag shown in the UI was not persisted.",
                        name, r.status_code,
                    )
                else:
                    logger.info("API: Tags updated for '%s'", name)
            except Exception as e:
                logger.error("API: Tag error for '%s': %s", name, e)

        self._submit_api(_worker, "update-tags")

    def update_favorite(self, item_id: str, state: bool) -> None:
        def _worker() -> None:
            try:
                path = (
                    f"/Users/{self.client.user_id}/FavoriteItems/{item_id}"
                )
                r = (self.client.post if state else self.client.delete)(
                    path, timeout=7
                )
                if r.status_code >= 300:
                    logger.error(
                        "API: Favorite toggle REJECTED for %s (HTTP %d) — "
                        "the state shown in the UI was not persisted.",
                        item_id, r.status_code,
                    )
                else:
                    logger.info(
                        "API: Favorite toggled for %s -> %s", item_id, state
                    )
            except Exception as e:
                logger.error("API: Favorite error: %s", e)

        self._submit_api(_worker, "update-favorite")

    # ── stats ─────────────────────────────────────────────────────────────

    def _toggle_stats_overlay(self) -> None:
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
            logger.warning(
                "Stats overlay toggle failed (stats.lua not loaded?): %s", e
            )

    def _dump_stats_json(self) -> None:
        cells_payload = []
        for i, c in enumerate(self.cells):
            cells_payload.append({
                "cell": i,
                "totals": dict(c._stats_total),
                "info": {k: v for k, v in c._stats_info.items()},
                "freezes": c._freeze_count,
                "freeze_seconds": round(c._freeze_total_s, 1),
                "postseek_refills": c._freeze_postseek_count,
                "last_item": (c.current_item or {}).get("Name"),
            })
        payload = {
            "ts": _time.strftime("%Y-%m-%dT%H:%M:%S"),
            "n_cells": len(self.cells),
            "mpv_opts_effective": apply_env_overrides(MPV_OPTS),
            "env": {
                k: os.environ.get(k)
                for k in (
                    "HYPERWALL_STATS", "HYPERWALL_HDR_HINT",
                    "HYPERWALL_HWDEC", "HYPERWALL_GPU_API",
                    "HYPERWALL_PROFILE", "HYPERWALL_VIDEO_SYNC",
                )
                if os.environ.get(k) is not None
            },
            "cells": cells_payload,
        }
        out = os.path.join(
            SCRIPT_DIR, f"hyperwall_stats_{int(_time.time())}.json"
        )
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
                "STATS cell %d  drop=%g  mistimed=%g  vo-delayed=%g  "
                "dec-drop=%g  freezes=%d(%ss)  postseek=%d  hwdec=%s  fps=%s  bitrate=%s",
                s["cell"],
                t.get("frame-drop-count", 0),
                t.get("mistimed-frame-count", 0),
                t.get("vo-delayed-frame-count", 0),
                t.get("decoder-frame-drop-count", 0),
                s["freezes"], s["freeze_seconds"], s["postseek_refills"],
                i.get("hwdec-current"),
                i.get("estimated-vf-fps") or i.get("container-fps"),
                i.get("video-bitrate"),
            )

    # ── shutdown ──────────────────────────────────────────────────────────

    def _shutdown(self) -> None:
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        logger.info("Shutdown requested.")
        self._cleanup()
        QApplication.instance().quit()

    def _cleanup(self) -> None:
        if self._cleaned_up:
            return
        self._cleaned_up = True

        # darwin: free each cell's mpv render context HERE — synchronously,
        # on the GUI thread, while the native windows still exist — BEFORE
        # the pool below terminates the mpv cores. render.h: freeing after
        # core destruction is UB. The old design queued the free onto the
        # GUI thread from the pool, but the GUI thread was blocked waiting
        # ON the pool → terminate ran with a live render context → SIGABRT
        # at exit (M5 Air 2026-07-21, third exit crash).
        import sys as _sys
        if _sys.platform == "darwin":
            for c in self.cells:
                try:
                    c.video_frame.release()
                except Exception as e:
                    logger.debug("GL pre-release failed: %s", e)

        # Hide all windows immediately
        for w in self.windows:
            try:
                w.hide()
            except Exception:
                pass

        # Stop all Emby sessions
        for c in self.cells:
            self.stop_emby_session(c._emby_item_id, c._emby_session_id)
            # A queued prefetch may have opened its stream server-side
            # (mpv pre-opens the demuxer near the current track's EOF).
            if c._prefetched is not None:
                p_item, _p_url, p_sid = c._prefetched
                self.stop_emby_session(p_item.get("Id"), p_sid)

        # Flush stats
        if STATS_ENABLED:
            for c in self.cells:
                try:
                    c._flush_stats()
                except Exception as e:
                    logger.warning("stats flush failed: %s", e)

        # Terminate all mpv instances in parallel, with a wait that is
        # actually bounded: no `with` block here, because the executor's
        # __exit__ calls shutdown(wait=True) and would block past the timeout
        # on any wedged terminate. Leftover workers are abandoned instead.
        import concurrent.futures as _cf
        if self.cells:
            ex = _cf.ThreadPoolExecutor(max_workers=min(len(self.cells), 32))
            futures = [ex.submit(c.release) for c in self.cells]
            done, not_done = _cf.wait(futures, timeout=5.0)
            for f in done:
                if f.exception():
                    logger.warning("Cell release failed: %s", f.exception())
            if not_done:
                logger.warning(
                    "%d cell release(s) still running after 5s — abandoning.",
                    len(not_done),
                )
            ex.shutdown(wait=False)

        if STATS_ENABLED:
            self._dump_stats_json()

        # Drain API pool
        self._api_pool_closed = True
        import threading as _threading
        _drain = _threading.Thread(
            target=self._api_pool.shutdown,
            kwargs={"wait": True},
            daemon=True,
        )
        _drain.start()
        _drain.join(timeout=6.0)
        if _drain.is_alive():
            logger.warning("API pool drain timed out — forcing shutdown.")

        try:
            QApplication.instance().removeEventFilter(self._escape_filter)
        except Exception as e:
            logger.debug("removeEventFilter failed: %s", e)

        self.client.close()
        logger.info("Cleanup complete.")
