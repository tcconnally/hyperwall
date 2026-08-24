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
import threading
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
    DisplayRole,
    OUTAGE_MIN_CELLS,
    OUTAGE_WINDOW_S,
    SESSION_CLEANUP_RETRY_S,
    PREFETCH_MIN_INTERVAL_MS,
    STREAM_START_STAGGER_MS,
    STATS_ENABLED,
    STATS_COUNTER_PROPS,
    STATS_INFO_PROPS,
    TRANSCODE_PREFETCH_RETRY_ATTEMPTS,
    TRANSCODE_PREFETCH_RETRY_S,
    apply_cache_budget,
    apply_env_overrides,
    effective_bitrate_budget_mbps,
    MPV_OPTS,
    normalize_display_layout,
    SCRIPT_DIR,
)
from .emby import EmbyClient, ContentLoader
from .urls import needs_transcode as _needs_transcode_pure
from .reliability import (
    allow_transcode_prefetch,
    gate_auto_transcode,
    is_systemic_outage,
    PlaybackToken,
    prefetch_slot,
    transcode_load_count,
)
from .urls import build_stream_url, tag_names
from .playlist import PlaylistManager, DEFAULT_GROUP

logger = logging.getLogger("HyperWall")


class WallWindow(QMainWindow):
    """Fullscreen window for one display; notifies controller on resize so a
    soloed cell continues to fill the central widget."""

    def __init__(self, controller: "WallController"):
        super().__init__()
        self._controller = controller

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._controller._solo_window is self:
            cell = self._controller._solo_cell
            if cell is not None:
                cell.setGeometry(self.centralWidget().rect())


class EmergencyKeyFilter(QObject):
    """App-level escape handler — works even when mpv children steal focus.

    If a preview/wall cell is currently in solo full-screen mode, Escape
    exits solo first; a second Escape shuts the wall down.
    """

    def __init__(
        self,
        shutdown_callback: callable,
        solo_active_callback: callable | None = None,
        exit_solo_callback: callable | None = None,
    ):
        super().__init__()
        self._shutdown_callback = shutdown_callback
        self._solo_active = solo_active_callback or (lambda: False)
        self._exit_solo = exit_solo_callback or (lambda: None)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            if self._solo_active():
                self._exit_solo()
                return True
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
        display_roles: dict[str, str] | None = None,
        display_layouts: dict[str, dict[str, Any]] | None = None,
        preview_rows: int = 3,
        preview_cols: int = 4,
    ):
        self.client = client
        self.screens = screens
        self.libraries = libraries
        self.grid_rows = grid_rows
        self.grid_cols = grid_cols
        self.preview_rows = preview_rows
        self.preview_cols = preview_cols
        self.display_roles = display_roles or {}
        self.display_layouts = display_layouts or {}

        self.cells: list[VideoCell] = []
        self.windows: list[QMainWindow] = []
        # Per-window metadata: role, grid cells, solo state, layout, etc.
        self._window_meta: dict[int, dict[str, Any]] = {}
        self._solo_cell: VideoCell | None = None
        self._solo_window: QMainWindow | None = None
        self._sync: Any | None = None
        self._sync_enabled = False
        self._shortcuts: list[QShortcut] = []
        self.all_items: list[dict[str, Any]] = []
        self.filtered: list[dict[str, Any]] = []
        self.filter_mode = "all"  # explicit mode; avoids O(n) list compares
        # Per-source-group playout. Default: a single "all" group shared by
        # every cell → identical to the prior single global shuffled deque
        # (global de-dup until the pool is exhausted). Per-monitor sourcing
        # (Epic 4) assigns cells to different groups.
        self.playlists = PlaylistManager()
        # Session-scoped quarantine set: item IDs that starved (or exhausted
        # decoder recovery) this session are skipped by every playlist draw.
        self._starvation_quarantined: set[str] = set()
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
        self._session_registry: dict[str, str] = {}
        self._session_registry_limit = max(
            64, len(screens) * max(1, grid_rows * grid_cols) * 8,
        )
        self._session_cleanup_limit = max(
            128, self._session_registry_limit * 2,
        )
        self._session_cleanup_ledger: dict[str, str] = {}
        self._session_lock = threading.Lock()
        self._session_stop_inflight: set[str] = set()
        self._stopped_session_ids: deque[str] = deque(maxlen=4096)
        self._session_cleanup_timer = QTimer()
        self._session_cleanup_timer.setInterval(SESSION_CLEANUP_RETRY_S * 1000)
        self._session_cleanup_timer.timeout.connect(
            self._retry_deferred_session_cleanup
        )
        self._session_cleanup_timer.start()

        # Cross-thread → GUI-thread marshaling (used by the web remote).
        # Must be constructed on the main thread so queued calls land here.
        self._invoker = MainThreadInvoker()

        # Systemic-outage tracking: (monotonic ts, cell id) per playback
        # failure, consulted by cells to decide whether to escalate.
        self._failure_events: deque[tuple[float, int]] = deque(maxlen=512)
        self._last_outage_log_ts = 0.0
        # Monotonic watermark for global queued-prefetch admission. This is
        # intentionally controller-wide: all cells share the same link/cache
        # pressure, so per-cell timers cannot prevent a wall-wide burst.
        self._prefetch_next_ready_ts = 0.0
        self._mpv_opts_effective: dict[str, Any] = {}

        # Emergency escape
        self._escape_filter = EmergencyKeyFilter(
            self._shutdown,
            solo_active_callback=lambda: self._solo_cell is not None,
            exit_solo_callback=self._exit_solo,
        )
        QApplication.instance().installEventFilter(self._escape_filter)

        self._build_displays()

        # Memory-aware demuxer budget: now that every cell exists, scale the
        # per-cell demuxer cache so the grid total stays under CACHE_BUDGET_MB.
        n_cells = len(self.cells)
        budgeted = apply_cache_budget(apply_env_overrides(MPV_OPTS), n_cells)
        self._mpv_opts_effective = dict(budgeted)
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

    # ── display construction ───────────────────────────────────────────────────────

    def _build_displays(self) -> None:
        for screen in self.screens:
            role = self.display_roles.get(
                screen.name(), DisplayRole.WALL
            )
            if role not in DisplayRole._ALL:
                role = DisplayRole.WALL
            is_preview = role == DisplayRole.PREVIEW
            default_rows = self.preview_rows if is_preview else self.grid_rows
            default_cols = self.preview_cols if is_preview else self.grid_cols
            raw_layout = self.display_layouts.get(screen.name(), {})
            if not isinstance(raw_layout, dict):
                raw_layout = {}
            layout = normalize_display_layout({
                "rotation": raw_layout.get("rotation", "auto"),
                "rows": raw_layout.get("rows", default_rows),
                "cols": raw_layout.get("cols", default_cols),
            })
            rotation = str(layout["rotation"])
            rows = int(layout["rows"])
            cols = int(layout["cols"])

            win = WallWindow(self)
            role_name = "Preview" if is_preview else "Wall"
            win.setWindowTitle(f"HyperWall — {role_name} — {screen.name()}")
            win.setStyleSheet("background: black;")

            cw = QWidget()
            win.setCentralWidget(cw)
            grid = QGridLayout(cw)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(0)

            window_cells: list[VideoCell] = []
            cell_positions: dict[int, tuple[int, int]] = {}
            display_id = uuid.uuid4().hex
            for r in range(rows):
                for c in range(cols):
                    cell = VideoCell(self)
                    cell.cell_id = uuid.uuid4().hex
                    cell.request_next.connect(self.next_video)
                    cell.request_prev.connect(self.prev_video)
                    cell.resource_quarantined.connect(self._on_resource_quarantined)
                    cell.request_solo.connect(self._toggle_solo)
                    cell.request_remote_solo.connect(self._remote_solo)
                    grid.addWidget(cell, r, c)
                    self.cells.append(cell)
                    window_cells.append(cell)
                    cell_positions[id(cell)] = (r, c)

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
            self._window_meta[id(win)] = {
                "screen": screen,
                "role": role,
                "rotation": rotation,
                "rows": rows,
                "cols": cols,
                "display_id": display_id,
                "grid": grid,
                "cells": window_cells,
                "positions": cell_positions,
                "solo": False,
            }
            logger.info(
                "Display built: %s (%s, rotation=%s, %dx%d)",
                screen.name(), role_name, rotation, rows, cols,
            )

    def _window_for_cell(self, cell: VideoCell) -> QMainWindow | None:
        """Return the QMainWindow that contains the given cell."""
        parent = cell.parentWidget()
        while parent is not None:
            if isinstance(parent, QMainWindow):
                return parent
            parent = parent.parentWidget()
        return None

    def _toggle_solo(self, cell: VideoCell) -> None:
        """Double-click handler: enter or exit full-screen solo for a cell.

        Solo is only enabled on PREVIEW displays so the public wall grid
        stays intact; double-clicking a wall cell is ignored.
        """
        win = self._window_for_cell(cell)
        if win is None:
            return
        meta = self._window_meta.get(id(win))
        if meta is None or meta["role"] != DisplayRole.PREVIEW:
            return
        if self._solo_cell is cell:
            self._exit_solo()
            return
        if self._solo_cell is not None:
            self._exit_solo()
        self._enter_solo(cell)

    def _remote_solo(self, cell: VideoCell) -> None:
        """Ctrl+double-click handler: ask the sync server to solo this item
        on all other peers' displays."""
        if not self._sync_enabled or self._sync is None:
            return
        item_id = (cell.current_item or {}).get("Id")
        if not item_id:
            return
        self.sync_broadcast({"type": "remote_solo", "item_id": item_id})
        logger.info("Remote solo requested for item %s", item_id)

    def _enter_solo(self, cell: VideoCell) -> None:
        win = self._window_for_cell(cell)
        if win is None:
            return
        meta = self._window_meta.get(id(win))
        if meta is None or meta["solo"]:
            return

        grid: QGridLayout = meta["grid"]
        # Remove the cell from the grid (keeps parent) and hide the rest.
        grid.removeWidget(cell)
        for other in meta["cells"]:
            if other is not cell:
                other.hide()
        cell.setParent(win.centralWidget())
        cell.setGeometry(win.centralWidget().rect())
        cell.show()
        cell.raise_()

        meta["solo"] = True
        self._solo_cell = cell
        self._solo_window = win
        did = meta.get("display_id")
        cid = getattr(cell, "cell_id", None)
        if did and cid:
            self.sync_broadcast_solo(did, cid)
        logger.info("Solo: cell %d full-screen on %s", self.cells.index(cell), win.windowTitle())

    def _exit_solo(self) -> None:
        """Restore the soloed cell into its grid position."""
        cell = self._solo_cell
        win = self._solo_window
        if cell is None or win is None:
            return
        meta = self._window_meta.get(id(win))
        if meta is None:
            return

        grid: QGridLayout = meta["grid"]
        pos = meta["positions"].get(id(cell))
        if pos is None:
            return

        cell.setParent(win.centralWidget())
        grid.addWidget(cell, pos[0], pos[1])
        for other in meta["cells"]:
            other.show()

        did = meta.get("display_id")
        meta["solo"] = False
        self._solo_cell = None
        self._solo_window = None
        if did:
            self.sync_broadcast_exit_solo(did)
        logger.info("Solo: exited")

    # ── network sync ──────────────────────────────────────────────────────────────────

    def set_sync_adapter(self, sync: Any) -> None:
        """Attach a SyncServer or SyncClient after construction."""
        self._sync = sync
        self._sync_enabled = sync is not None

    def sync_broadcast(self, msg: dict[str, Any]) -> None:
        """Send a state change to peers if sync is active."""
        if not self._sync_enabled or self._sync is None:
            return
        try:
            self._sync.broadcast(msg)
        except Exception as e:
            logger.debug("Sync broadcast failed: %s", e)

    def sync_broadcast_cell_update(self, cell: VideoCell) -> None:
        if not self._sync_enabled:
            return
        cid = getattr(cell, "cell_id", None)
        iid = (cell.current_item or {}).get("Id")
        if cid and iid:
            self.sync_broadcast({
                "type": "cell_update",
                "cell_id": cid,
                "item_id": iid,
            })

    def sync_broadcast_solo(self, display_id: str, cell_id: str) -> None:
        if not self._sync_enabled:
            return
        self.sync_broadcast({
            "type": "solo",
            "display_id": display_id,
            "cell_id": cell_id,
        })

    def sync_broadcast_exit_solo(self, display_id: str) -> None:
        if not self._sync_enabled:
            return
        self.sync_broadcast({
            "type": "exit_solo",
            "display_id": display_id,
        })

    def sync_broadcast_filter(self) -> None:
        if not self._sync_enabled:
            return
        self.sync_broadcast({
            "type": "filter",
            "mode": self.filter_mode,
        })

    def sync_apply(self, msg: dict[str, Any]) -> None:
        """Apply a remote sync message on the GUI thread."""
        mtype = msg.get("type")
        if mtype == "cell_update":
            self._sync_apply_cell_update(msg)
        elif mtype == "solo":
            self._sync_apply_solo(msg)
        elif mtype == "exit_solo":
            self._sync_apply_exit_solo(msg)
        elif mtype == "filter":
            self._sync_apply_filter(msg)
        elif mtype == "full_state":
            self._sync_apply_full_state(msg)

    def _sync_find_cell(self, cell_id: str) -> VideoCell | None:
        for cell in self.cells:
            if getattr(cell, "cell_id", None) == cell_id:
                return cell
        return None

    def _sync_find_display(self, display_id: str) -> QMainWindow | None:
        for win in self.windows:
            meta = self._window_meta.get(id(win))
            if meta and meta.get("display_id") == display_id:
                return win
        return None

    def _sync_apply_cell_update(self, msg: dict[str, Any]) -> None:
        cid = msg.get("cell_id")
        iid = msg.get("item_id")
        if not cid or not iid:
            return
        cell = self._sync_find_cell(cid)
        if cell is None:
            return
        # Avoid re-applying our own broadcasts.
        current_iid = (cell.current_item or {}).get("Id")
        if current_iid == iid:
            return
        item = next((i for i in self.all_items if i.get("Id") == iid), None)
        if item is None:
            # Item not in our local library yet; load may still be in progress.
            logger.debug("Sync cell_update for unknown item %s", iid)
            return
        self._hand_off(cell, item)

    def _sync_apply_solo(self, msg: dict[str, Any]) -> None:
        did = msg.get("display_id")
        cid = msg.get("cell_id")
        iid = msg.get("item_id")
        if not did:
            return
        win = self._sync_find_display(did)
        if win is None:
            return
        cell = None
        if cid:
            cell = self._sync_find_cell(cid)
        # If the message carried an item_id instead of (or in addition to) a
        # cell_id, load that item into the target display's first cell.
        if cell is None and iid:
            meta = self._window_meta.get(id(win))
            if meta and meta["cells"]:
                target = meta["cells"][0]
                item = next((i for i in self.all_items if i.get("Id") == iid), None)
                if item is not None:
                    self._hand_off(target, item)
                    cell = target
        if cell is None:
            return
        if self._solo_cell is not None:
            self._exit_solo()
        self._enter_solo(cell)

    def _sync_apply_exit_solo(self, msg: dict[str, Any]) -> None:
        did = msg.get("display_id")
        if not did:
            return
        if self._solo_window is None:
            return
        meta = self._window_meta.get(id(self._solo_window))
        if meta and meta.get("display_id") == did:
            self._exit_solo()

    def _sync_apply_filter(self, msg: dict[str, Any]) -> None:
        mode = msg.get("mode")
        if mode in ("all", "favorites") and mode != self.filter_mode:
            self._set_filter(mode)

    def _sync_apply_full_state(self, msg: dict[str, Any]) -> None:
        cells = msg.get("cells", {})
        for cid, iid in cells.items():
            self._sync_apply_cell_update({"cell_id": cid, "item_id": iid})
        solo = msg.get("solo", {})
        if solo.get("display_id"):
            self._sync_apply_solo(solo)
        mode = msg.get("filter")
        if mode:
            self._sync_apply_filter({"mode": mode})

    # ── content loading ─────────────────────────────────────────────────────────────

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

    def _transcode_load_count(
        self,
        cell: "VideoCell | None" = None,
        include_cell: bool = False,
    ) -> int:
        """Count current and queued HLS loads consuming server capacity.

        A pending mpv playlist entry can already have opened its HLS demuxer,
        so it must count against the transcode ceiling even before playback
        advances to it.  A replacement load excludes its own old cell state;
        a prefetch includes the target cell's current and queued state.
        """
        streams = []
        for other in self.cells:
            if other is cell and not include_cell:
                continue
            streams.append((getattr(other, "_stream_url", None), False))
            streams.append((getattr(other, "_prefetched_stream_url", None), True))
        return transcode_load_count(streams)

    def _auto_transcode_requested(self, item: dict[str, Any]) -> bool:
        """Return whether the item exceeds the configured direct-play budget."""
        return _needs_transcode_pure(
            item,
            auto_transcode=os.environ.get(
                "HYPERWALL_AUTO_TRANSCODE", "1") == "1",
            max_fps=MAX_DIRECT_FPS,
            max_bitrate_mbps=self._bitrate_budget_mbps,
        )

    def _build_url(
        self, item: dict[str, Any], force_transcode: bool = False,
        prefetch: bool = False, cell: "VideoCell | None" = None,
    ) -> tuple[str, str]:
        iid = item["Id"]
        key = self.client.access_token
        base = self.client.server_url
        sid = uuid.uuid4().hex

        auto_transcode = self._auto_transcode_requested(item)
        # Concurrency gate: a forced retry (failed direct) must transcode, but
        # an AUTO escalation defers to direct-play when the transcode engine is
        # already busy — never stampede greg's media engine (2026-07-15).
        gated = False
        if force_transcode:
            transcode = True
        else:
            occupied = self._transcode_load_count(
                cell=cell,
                include_cell=prefetch,
            )
            transcode = gate_auto_transcode(
                auto_transcode, occupied, MAX_CONCURRENT_TRANSCODES,
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

    def _retain_session_cleanup_locked(
        self, item_id: str, session_id: str,
    ) -> None:
        ledger = self._session_cleanup_ledger
        # A stop request can target a still-active registry entry. Keep a
        # second pending record so the retry timer can revisit it without
        # dropping the durable active-session record prematurely.
        if session_id in ledger:
            ledger[session_id] = item_id
            return
        if len(ledger) >= self._session_cleanup_limit:
            logger.critical(
                "Session cleanup capacity exhausted; admission must remain "
                "closed until an unresolved stop succeeds."
            )
            return
        ledger[session_id] = item_id

    def _session_admission_available(self) -> bool:
        with self._session_lock:
            return (
                len(set(self._session_registry)
                    | set(self._session_cleanup_ledger))
                < self._session_cleanup_limit
            )

    def _retry_deferred_session_cleanup(self) -> None:
        """Retry pending server-session stops after outages or failures."""
        if self._shutdown_requested or self._cleaned_up:
            return
        with self._session_lock:
            pending = list(self._session_cleanup_ledger.items())
        for session_id, item_id in pending:
            self.stop_emby_session(item_id, session_id)


    def _register_session(self, item_id: str | None, session_id: str | None) -> bool:
        if not item_id or not session_id:
            return False
        evicted: list[tuple[str, str]] = []
        with self._session_lock:
            is_new = (
                session_id not in self._session_registry
                and session_id not in self._session_cleanup_ledger
            )
            if (
                is_new
                and len(set(self._session_registry)
                    | set(self._session_cleanup_ledger))
                >= self._session_cleanup_limit
            ):
                logger.error("Session admission closed at cleanup capacity.")
                return False
            self._session_registry[session_id] = item_id
            self._session_cleanup_ledger.pop(session_id, None)
            try:
                self._stopped_session_ids.remove(session_id)
            except ValueError:
                pass
            limit = getattr(self, "_session_registry_limit", 4096)
            while len(self._session_registry) > limit:
                old_session, old_item = next(iter(self._session_registry.items()))
                self._session_registry.pop(old_session, None)
                self._retain_session_cleanup_locked(old_item, old_session)
                evicted.append((old_item, old_session))
        for old_item, old_session in evicted:
            logger.error(
                "Session registry bound reached; forcing cleanup for %s",
                old_session[:8],
            )
            self.stop_emby_session(old_item, old_session)
        return True

    def stop_emby_session(
        self, item_id: str | None, session_id: str | None,
    ) -> None:
        if not item_id or not session_id:
            return
        with self._session_lock:
            if (
                session_id in self._stopped_session_ids
                or session_id in self._session_stop_inflight
            ):
                return
            # During a systemic outage, skip the API call entirely — every
            # stop-session POST would time out (5s connect + 5s retry), and
            # with only 4 pool workers the wall can't admit new sessions.
            # Retain in the cleanup ledger for a future retry or shutdown.
            if self.in_outage():
                self._retain_session_cleanup_locked(item_id, session_id)
                logger.debug(
                    "Stop-session %s deferred during systemic outage.",
                    session_id[:8],
                )
                return
            self._session_stop_inflight.add(session_id)

        def _worker() -> None:
            success = False
            last_error: Exception | None = None
            for attempt in range(2):
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
                    if 200 <= r.status_code < 300:
                        success = True
                        logger.info(
                            "Session stop %s -> HTTP %d",
                            session_id[:8], r.status_code,
                        )
                        break
                    last_error = RuntimeError(
                        f"HTTP {r.status_code} from stop-session"
                    )
                    logger.warning(
                        "Stop-session %s rejected (attempt %d/2): HTTP %d",
                        session_id[:8], attempt + 1, r.status_code,
                    )
                except Exception as e:
                    last_error = e
                    logger.warning(
                        "Stop-session %s failed (attempt %d/2): %s",
                        session_id[:8], attempt + 1, e,
                    )
                if attempt == 0:
                    _time.sleep(0.1)
            with self._session_lock:
                self._session_stop_inflight.discard(session_id)
                if success:
                    self._session_registry.pop(session_id, None)
                    self._session_cleanup_ledger.pop(session_id, None)
                    self._stopped_session_ids.append(session_id)
                else:
                    self._retain_session_cleanup_locked(item_id, session_id)
            if not success and last_error is not None:
                logger.error(
                    "Stop-session %s remains registered after retries: %s",
                    session_id[:8], last_error,
                )

        future = self._submit_api(_worker, "stop-session")
        if future is None:
            with self._session_lock:
                self._session_stop_inflight.discard(session_id)
                self._retain_session_cleanup_locked(item_id, session_id)

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
        preserve_failure_state: bool = False,
    ) -> None:
        old_item_id = cell._emby_item_id
        old_session_id = cell._emby_session_id
        if not self._session_admission_available():
            self.playlists.push_front(self._cell_group(cell), item)
            logger.error("Session admission closed; requeued handoff item.")
            return
        url, sid = self._build_url(item, force_transcode, cell=cell)

        def _on_started(started: bool) -> None:
            if not started:
                # The candidate may have been rejected during shutdown or a
                # native load failure. Do not stop the still-playing old
                # session; only clean up the candidate we created here.
                self.stop_emby_session(item.get("Id"), sid)
                return
            if not self._register_session(item.get("Id"), sid):
                self.stop_emby_session(item.get("Id"), sid)
                return
            cell._emby_session_id = sid
            cell._emby_item_id = item["Id"]
            if old_session_id and old_session_id != sid:
                self.stop_emby_session(old_item_id, old_session_id)
            self._arm_prefetch(cell)

        # play() invokes the callback immediately or carries it through a
        # deferred lock admission. Session ownership changes only after the
        # replacement has actually been admitted.
        cell.play(
            item, url,
            preserve_failure_state=preserve_failure_state,
            on_started=_on_started,
            session_id=sid,
        )

    def _arm_prefetch(self, cell: VideoCell) -> None:
        """Schedule a token-checked, globally paced playlist warmup."""
        token = cell._current_playback_token()
        if token is None or cell._prefetch_request_token == token:
            return
        cell._prefetch_request_token = token

        def _queue(token: PlaybackToken = token) -> None:
            if cell._prefetch_request_token != token:
                return
            cell._prefetch_request_token = None
            if (
                self._shutdown_requested
                or cell._mpv is None
                or not cell._playback_token_is_current(token)
            ):
                return
            delay_s, self._prefetch_next_ready_ts = prefetch_slot(
                _time.monotonic(),
                self._prefetch_next_ready_ts,
                interval_s=PREFETCH_MIN_INTERVAL_MS / 1000.0,
            )
            if delay_s <= 0.0:
                self._do_prefetch_if_current(cell, token)
                return
            QTimer.singleShot(
                max(1, int(delay_s * 1000)),
                lambda c=cell, t=token: self._do_prefetch_if_current(c, t),
            )

        QTimer.singleShot(0, _queue)

    def _do_prefetch_if_current(
        self, cell: VideoCell, token: PlaybackToken,
    ) -> None:
        """Run a reserved prefetch only if its playback identity survived."""
        if (
            self._shutdown_requested
            or cell._mpv is None
            or not cell._playback_token_is_current(token)
        ):
            return
        self._do_prefetch(cell, token)

    def _do_prefetch(
        self,
        cell: VideoCell,
        token: PlaybackToken,
        *,
        defer_on_saturation: bool = True,
    ) -> None:
        """Draw, admit, and prefetch the cell's next item.

        Shared by the normal post-advance arm (_queue) and the deferred
        transcode retry. defer_on_saturation: when every transcode slot is
        busy, requeue the item and retry on a timer (the 2026-08-09 soak
        showed a dropped prefetch cold-starts at advance → starvation);
        otherwise log the classic skip and leave the item queued.
        """
        item = self.playlists.next(
            self._cell_group(cell),
            skip_ids=self._starvation_quarantined,
        )
        if item is None:
            return
        # Decide admission before building the URL. _build_url intentionally
        # demotes an over-budget AUTO transcode to DIRECT for active
        # playback; a prefetch must instead remain queued so it does not
        # consume an item while bypassing the transcode ceiling.
        occupied = self._transcode_load_count(
            cell=cell,
            include_cell=True,
        )
        if self._auto_transcode_requested(item) and not allow_transcode_prefetch(
            occupied, MAX_CONCURRENT_TRANSCODES,
        ):
            self.playlists.push_front(
                self._cell_group(cell), item,
            )
            if defer_on_saturation:
                logger.info(
                    "Deferring transcoded prefetch while %d/%d "
                    "transcode slots are active (retry in %ds).",
                    occupied, MAX_CONCURRENT_TRANSCODES,
                    TRANSCODE_PREFETCH_RETRY_S,
                )
                self._schedule_transcode_prefetch_retry(cell, token, item)
            else:
                logger.info(
                    "Skipping transcoded prefetch while %d/%d "
                    "transcode slots are active.",
                    occupied, MAX_CONCURRENT_TRANSCODES,
                )
            return
        if not self._session_admission_available():
            self.playlists.push_front(self._cell_group(cell), item)
            logger.error("Session admission closed; requeued prefetch item.")
            return
        try:
            url, sid = self._build_url(item, prefetch=True, cell=cell)
        except Exception as e:
            self.playlists.push_front(
                self._cell_group(cell), item,
            )
            logger.debug("Prefetch URL build declined: %s", e)
            return
        if not cell.prefetch(item, url, sid):
            self.playlists.push_front(
                self._cell_group(cell), item,
            )
            self.stop_emby_session(item.get("Id"), sid)
            logger.debug("Prefetch declined for %s.", item.get("Name", "?"))
        else:
            if not self._register_session(item.get("Id"), sid):
                cell.drop_prefetch(requeue=True)
                self.stop_emby_session(item.get("Id"), sid)

    def _schedule_transcode_prefetch_retry(
        self,
        cell: VideoCell,
        token: PlaybackToken,
        item: dict,
        attempt: int = 1,
    ) -> None:
        """Retry a slot-saturated transcode prefetch once a slot frees.

        Bounded by TRANSCODE_PREFETCH_RETRY_ATTEMPTS (disabled entirely when
        the interval/attempts env is 0, restoring the old skip). Each retry
        re-checks that the cell still wants this playback token and that the
        item is still the group's next candidate — if the cell advanced or
        another draw consumed the item, the retry stops.
        """
        if (
            TRANSCODE_PREFETCH_RETRY_S <= 0
            or TRANSCODE_PREFETCH_RETRY_ATTEMPTS <= 0
            or attempt > TRANSCODE_PREFETCH_RETRY_ATTEMPTS
        ):
            return

        def _retry(attempt: int = attempt) -> None:
            if (
                self._shutdown_requested
                or cell._mpv is None
                or not cell._playback_token_is_current(token)
            ):
                return
            if (
                cell._prefetch_request_token is not None
                and cell._prefetch_request_token != token
            ):
                return  # a newer prefetch request owns the cell
            if self.playlists.peek(self._cell_group(cell)) is not item:
                return  # superseded or consumed by another draw
            occupied = self._transcode_load_count(
                cell=cell,
                include_cell=True,
            )
            if self._auto_transcode_requested(item) and not allow_transcode_prefetch(
                occupied, MAX_CONCURRENT_TRANSCODES,
            ):
                if attempt < TRANSCODE_PREFETCH_RETRY_ATTEMPTS:
                    logger.info(
                        "Transcoded prefetch still deferred "
                        "(%d/%d slots) — retry %d/%d.",
                        occupied, MAX_CONCURRENT_TRANSCODES,
                        attempt, TRANSCODE_PREFETCH_RETRY_ATTEMPTS,
                    )
                    self._schedule_transcode_prefetch_retry(
                        cell, token, item, attempt + 1,
                    )
                return
            # A slot is free — prefetch now. defer_on_saturation=False so a
            # re-saturation here ends the chain (classic skip, item stays
            # queued for a cold start rather than retrying forever).
            self._do_prefetch(cell, token, defer_on_saturation=False)

        QTimer.singleShot(
            TRANSCODE_PREFETCH_RETRY_S * 1000, _retry,
        )

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

    def in_outage(self) -> bool:
        """Return True when a systemic outage is currently active.

        Consults the sliding failure-event window. Used by session cleanup
        to skip doomed API calls that would only block the pool workers
        during a known network/server outage.
        """
        now = _time.monotonic()
        return is_systemic_outage(
            self._failure_events, now,
            window_s=OUTAGE_WINDOW_S,
            total_cells=len(self.cells),
            min_cells=OUTAGE_MIN_CELLS,
        )

    def _cell_group(self, cell: VideoCell) -> str:
        """Source group a cell draws from. Defaults to the shared 'all' group;
        per-monitor sourcing sets cell._source_group to a distinct key."""
        return getattr(cell, "_source_group", DEFAULT_GROUP) or DEFAULT_GROUP

    @traced("wall.next_video")
    def next_video(self, cell: VideoCell, is_retry: bool = False) -> None:
        if is_retry and cell.current_item:
            self._hand_off(
                cell,
                cell.current_item,
                cell._force_transcode,
                preserve_failure_state=True,
            )
            return
        prev = cell.current_item
        # A macOS prefetched advance is queued on a daemon worker. Ignore a
        # duplicate EOF/input event until its queued completion commits; the
        # completion owns history-adjacent cleanup, re-arm, and sync.
        if getattr(cell, "_prefetch_advance_inflight", None) is not None:
            logger.debug("Prefetched advance already in flight; ignoring next.")
            return
        # Fast path: the next item is already queued and warmed on this
        # cell playlist — the advance is a ~60ms cut, not a cold open.
        prefetched = cell._prefetched
        if cell.advance_to_prefetched():
            if prev:
                cell.history.append(prev)
            logger.info(
                "[PREFETCH→] %s",
                (prefetched[0] if prefetched else cell.current_item or {}).get("Name"),
            )
            return
        item = self.playlists.next(
            self._cell_group(cell),
            skip_ids=self._starvation_quarantined,
        )
        if item is None:
            return
        if prev:
            cell.history.append(prev)
        self._hand_off(cell, item)
        self.sync_broadcast_cell_update(cell)

    def _on_resource_quarantined(self, item: dict) -> None:
        """Add a quarantined resource to the session skip set."""
        if item and item.get("Id"):
            self._starvation_quarantined.add(item["Id"])

    @traced("wall.prev_video")
    def prev_video(self, cell: VideoCell) -> None:
        if cell.history:
            item = cell.history.pop()
            self._hand_off(cell, item)
            self.sync_broadcast_cell_update(cell)

    # ── global controls ───────────────────────────────────────────────────────────────────

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
        # The event-thread cache is deliberately used here; reading every
        # native pause property from the GUI races audio-arm ownership.
        any_playing = any(not c._paused for c in active_mpvs)
        for c in active_mpvs:
            c._set_pause_from_controller(any_playing)
        if not any_playing:
            # EOF state is also maintained by the event observer, so this
            # resume path never performs an unlocked native read.
            for c in active_mpvs:
                if c._eof_reached:
                    self.next_video(c, False)

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
        self.sync_broadcast_filter()

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
        if cell._run_native_commands(
            ("script-binding", "stats/display-stats-toggle"),
            ("script-binding", "stats/display-page-2"),
        ):
            logger.info("Stats overlay toggled on cell 0 (page 2).")
        else:
            logger.warning("Stats overlay toggle failed (stats.lua not loaded?).")

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
            "mpv_opts_effective": dict(self._mpv_opts_effective),
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
        report_dir = os.environ.get("HYPERWALL_SOAK_REPORT_DIR", "").strip()
        out = os.path.join(
            report_dir or SCRIPT_DIR,
            f"hyperwall_stats_{int(_time.time())}.json",
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
        shutdown_deadline = _time.monotonic() + 5.0
        deferred = self._cleanup(shutdown_deadline)
        if deferred:
            self._finish_deferred_shutdown(deferred, shutdown_deadline)
        else:
            QApplication.instance().quit()

    def _finish_deferred_shutdown(
        self, cells: list[VideoCell], deadline: float,
    ) -> None:
        pending = [
            c for c in cells
            if (
                not c._render_context_released
                or c._mpv is not None
                or c.has_pending_native_finalizer()
                or c.has_pending_render_finalizer()
            )
        ]
        for c in cells:
            if c._render_context_released and c._mpv is not None:
                c._destroy_mpv(
                    wait_s=max(0.0, deadline - _time.monotonic()),
                    shutdown_deadline=deadline,
                )
        if pending or any(
            c._mpv is not None
            or c.has_pending_native_finalizer()
            or c.has_pending_render_finalizer()
            for c in cells
        ):
            if _time.monotonic() < deadline:
                QTimer.singleShot(
                    25,
                    lambda: self._finish_deferred_shutdown(cells, deadline),
                )
                return
            logger.error(
                "Bounded shutdown deadline reached with deferred render/core "
                "resources; leaving them abandoned rather than crossing thread affinity."
            )
        QApplication.instance().quit()

    def _cleanup(self, shutdown_deadline: float | None = None) -> list[VideoCell]:
        shutdown_deadline = shutdown_deadline or (_time.monotonic() + 5.0)
        if self._cleaned_up:
            return []
        self._cleaned_up = True
        try:
            self._session_cleanup_timer.stop()
        except Exception as e:
            logger.debug("Session cleanup timer stop failed: %s", e)
        deferred_render_cells: list[VideoCell] = []
        prefetched_sessions = [
            (p_item.get("Id"), p_sid)
            for c in self.cells
            if c._prefetched is not None
            for p_item, _p_url, p_sid in (c._prefetched,)
        ]

        # Stop every Qt-owned timer/animation while still on the GUI thread.
        # The bounded worker pool below may release mpv cores, but it must not
        # call QTimer.stop() on widgets owned by this thread (Qt warns with
        # killTimer and leaves timers active during teardown).
        for c in self.cells:
            try:
                c.prepare_shutdown()
            except Exception as e:
                logger.debug("Cell GUI shutdown preparation failed: %s", e)

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
                    # Invalidate the worker before waiting for ownership. This
                    # lets an in-flight audio arm finish without targeting a
                    # replacement, while keeping GUI teardown bounded.
                    remaining = max(0.0, shutdown_deadline - _time.monotonic())
                    drained = c._cancel_audio_arm(
                        timeout_s=min(2.0, remaining),
                    )
                    if not drained:
                        logger.warning(
                            "Audio worker did not drain before GL release for cell."
                        )
                    remaining = max(0.0, shutdown_deadline - _time.monotonic())
                    if not c._audio_arm_call_lock.acquire(
                        timeout=min(2.0, remaining),
                    ):
                        logger.error(
                            "GL render release could not acquire native ownership "
                            "before timeout; deferring to the GUI shutdown drain."
                        )
                        c.request_render_release_when_idle(shutdown_deadline)
                        deferred_render_cells.append(c)
                        continue
                    try:
                        released = c._release_render_context_on_gui()
                    finally:
                        c._audio_arm_call_lock.release()
                    if not released:
                        c.request_render_release_when_idle(shutdown_deadline)
                        deferred_render_cells.append(c)
                except Exception as e:
                    logger.debug("GL pre-release failed: %s", e)
                    c.request_render_release_when_idle(shutdown_deadline)
                    if c not in deferred_render_cells:
                        deferred_render_cells.append(c)

        # Hide all windows immediately
        for w in self.windows:
            try:
                w.hide()
            except Exception:
                pass

        # Stop all Emby sessions
        for c in self.cells:
            self.stop_emby_session(c._emby_item_id, c._emby_session_id)
        for p_item_id, p_sid in prefetched_sessions:
            self.stop_emby_session(p_item_id, p_sid)
        with self._session_lock:
            registered_sessions = dict(self._session_cleanup_ledger)
            registered_sessions.update(self._session_registry)
        for session_id, item_id in registered_sessions.items():
            self.stop_emby_session(item_id, session_id)

        # Flush stats
        if STATS_ENABLED:
            for c in self.cells:
                try:
                    c._flush_stats(
                        timeout_s=max(0.0, shutdown_deadline - _time.monotonic()),
                    )
                except Exception as e:
                    logger.warning("stats flush failed: %s", e)

        # Terminate all mpv instances in parallel, with a wait that is
        # actually bounded: no `with` block here, because the executor's
        # __exit__ calls shutdown(wait=True) and would block past the timeout
        # on any wedged terminate. Leftover workers are abandoned instead.
        import concurrent.futures as _cf
        if self.cells:
            ex = _cf.ThreadPoolExecutor(max_workers=min(len(self.cells), 32))
            futures = [
                ex.submit(c.release, shutdown_deadline=shutdown_deadline)
                for c in self.cells
            ]
            remaining = max(0.0, shutdown_deadline - _time.monotonic())
            done, not_done = _cf.wait(futures, timeout=remaining)
            for f in done:
                if f.exception():
                    logger.warning("Cell release failed: %s", f.exception())
            if not_done:
                logger.warning(
                    "%d cell release(s) still running after 5s — abandoning.",
                    len(not_done),
                )
            ex.shutdown(wait=False)
            for c in self.cells:
                if (
                    (
                        c._mpv is not None
                        or c.has_pending_native_finalizer()
                        or c.has_pending_render_finalizer()
                    )
                    and c not in deferred_render_cells
                ):
                    deferred_render_cells.append(c)

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
        _drain.join(
            timeout=max(0.0, shutdown_deadline - _time.monotonic()),
        )
        if _drain.is_alive():
            logger.warning("API pool drain timed out — forcing shutdown.")

        try:
            QApplication.instance().removeEventFilter(self._escape_filter)
        except Exception as e:
            logger.debug("removeEventFilter failed: %s", e)

        if self._sync is not None:
            try:
                self._sync.stop()
            except Exception as e:
                logger.debug("Sync stop failed: %s", e)

        close_thread = _threading.Thread(
            target=self.client.close, name="client-close", daemon=True,
        )
        close_thread.start()
        close_thread.join(
            timeout=max(0.0, shutdown_deadline - _time.monotonic()),
        )
        if close_thread.is_alive():
            logger.warning("Client close exceeded global shutdown deadline.")
        logger.info("Cleanup complete.")
        return deferred_render_cells
