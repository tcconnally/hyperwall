"""
Hyperwall — SetupWizard.

Monitor + library + grid layout selection dialog.
"""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("HyperWall")

from . import VERSION_SHORT
from . import theme
from .constants import (
    DisplayRole,
    DisplayRotation,
    _s,
    normalize_display_layout,
)
from .displays import display_identity, restore_display_settings
from .wizard_logic import (
    grid_for_role_switch,
    resolve_saved_grid,
    update_last_selected_grid,
)


class _GridPreview(QWidget):
    """A tiny live diagram of the per-display grid (rows × cols of cells)."""

    def __init__(self, rows: int = 2, cols: int = 2):
        super().__init__()
        self._rows = rows
        self._cols = cols
        self.setFixedSize(_s(148), _s(92))

    def set_grid(self, rows: int, cols: int) -> None:
        self._rows, self._cols = max(1, rows), max(1, cols)
        self.update()

    def paintEvent(self, _event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        # bezel
        p.setBrush(QColor(theme.SURFACE_0))
        p.drawRoundedRect(self.rect(), _s(6), _s(6))
        gap = _s(3)
        pad = _s(6)
        w = self.width() - 2 * pad
        h = self.height() - 2 * pad
        cw = (w - gap * (self._cols - 1)) / self._cols
        ch = (h - gap * (self._rows - 1)) / self._rows
        p.setBrush(QColor(theme.ACCENT))
        for r in range(self._rows):
            for c in range(self._cols):
                x = pad + c * (cw + gap)
                y = pad + r * (ch + gap)
                p.drawRoundedRect(int(x), int(y), int(cw), int(ch), _s(2), _s(2))
        p.end()


class _DisplayRow(QWidget):
    """Interactive row used inside the display list.

    The row itself selects the monitor, while child combo boxes retain normal
    mouse handling. Making the whole row transparent would also make those
    child controls transparent in Qt, so selection is explicit instead.
    """

    clicked = pyqtSignal()

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class SetupWizard(QDialog):
    """Pre-launch configuration dialog: select monitors, libraries, and grids."""

    def __init__(
        self,
        screens: list[Any],
        libraries: list[str],
        last_screens: str = "",
        last_libraries: str = "",
        last_rows: int = 2,
        last_cols: int = 2,
        last_preview_rows: int = 3,
        last_preview_cols: int = 4,
        last_display_roles: dict[str, str] | None = None,
        last_display_layouts: dict[str, dict[str, object]] | None = None,
        last_display_settings: dict[str, dict[str, object]] | None = None,
    ):
        super().__init__()
        self.setWindowTitle(f"HyperWall {VERSION_SHORT}")
        self.resize(_s(1_160), _s(620))
        self.setStyleSheet(theme.dialog_qss())

        self._screen_map: dict[str, Any] = {}
        self._screen_items: dict[str, QListWidgetItem] = {}
        self._role_boxes: dict[str, QComboBox] = {}
        self._rotation_boxes: dict[str, QComboBox] = {}
        self._grid_boxes: dict[str, QComboBox] = {}
        self._preview_labels_by_item: dict[int, str] = {}
        self._grid_defaults = {
            DisplayRole.WALL: (last_rows, last_cols),
            DisplayRole.PREVIEW: (last_preview_rows, last_preview_cols),
        }
        self._last_selected_grids = dict(self._grid_defaults)
        last_display_roles = last_display_roles or {}
        last_display_layouts = last_display_layouts or {}
        self._saved_display_roles = dict(last_display_roles)
        self._saved_display_layouts = dict(last_display_layouts)
        self._saved_display_settings = dict(last_display_settings or {})
        # Stable-identity state is authoritative. The name-keyed maps are
        # retained only as a compatibility fallback for older config files.
        self._using_stable_settings = bool(self._saved_display_settings)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(_s(26), _s(22), _s(26), _s(22))
        layout.setSpacing(_s(14))

        # ── Header ──
        header = QVBoxLayout()
        header.setSpacing(0)
        title = QLabel("HYPERWALL")
        title.setStyleSheet(
            f"font-size: {_s(26)}px; font-weight: 900; color: {theme.TEXT};"
            f" letter-spacing: {_s(4)}px; background: transparent;"
        )
        subtitle = QLabel(f"video wall · v{VERSION_SHORT}")
        subtitle.setStyleSheet(
            f"font-size: {_s(11)}px; color: {theme.ACCENT}; letter-spacing: {_s(2)}px;"
            f" background: transparent;"
        )
        header.addWidget(title)
        header.addWidget(subtitle)
        layout.addLayout(header)

        panels = QHBoxLayout()
        panels.setSpacing(_s(14))

        # ── Displays ──
        grp_disp = QGroupBox("DISPLAYS · ROLE / ROTATION / GRID / PREVIEW")
        ld = QVBoxLayout(grp_disp)
        self.list_disp = QListWidget()
        self.list_disp.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection
        )
        prev_screens = last_screens.split(",") if last_screens else []

        for idx, s in enumerate(screens, 1):
            label_text = (
                f"Monitor {idx} — {s.name()}  "
                f"[{s.geometry().width()}x{s.geometry().height()}]"
            )
            item = QListWidgetItem()
            self.list_disp.addItem(item)
            self._screen_map[label_text] = s
            self._screen_items[label_text] = item
            screen_identity = display_identity(s)
            saved_settings = (
                restore_display_settings(
                    s,
                    self._saved_display_settings,
                    wall_grid=self._grid_defaults[DisplayRole.WALL],
                    preview_grid=self._grid_defaults[DisplayRole.PREVIEW],
                )
                if self._using_stable_settings
                else {}
            )
            self._preview_labels_by_item[id(item)] = label_text

            row = _DisplayRow()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(_s(6), _s(2), _s(6), _s(2))
            row_layout.setSpacing(_s(8))
            row.clicked.connect(
                lambda item=item: (
                    item.setSelected(True), self.list_disp.setCurrentItem(item)
                )
            )
            lbl = QLabel(label_text)
            lbl.setStyleSheet(
                f"color: {theme.TEXT}; font-size: {_s(11)}px; background: transparent;"
            )
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row_layout.addWidget(lbl, 1)

            role_box = QComboBox()
            role_box.addItem("Wall", DisplayRole.WALL)
            role_box.addItem("Preview", DisplayRole.PREVIEW)
            role_box.setMinimumWidth(_s(78))
            role_value = saved_settings.get("role", DisplayRole.WALL)
            if not self._using_stable_settings:
                role_value = last_display_roles.get(s.name(), DisplayRole.WALL)
            role = role_value if isinstance(role_value, str) else DisplayRole.WALL
            if not DisplayRole.is_valid(role):
                role = DisplayRole.WALL
            role_box.setCurrentIndex(0 if role == DisplayRole.WALL else 1)
            self._role_boxes[label_text] = role_box
            role_box.currentIndexChanged.connect(
                lambda _index, item=item, label=label_text: (
                    item.setSelected(True),
                    self.list_disp.setCurrentItem(item),
                    self._role_changed(label),
                    self._sync_selected_preview()
                )
            )
            row_layout.addWidget(role_box)

            rotation_box = QComboBox()
            for label, value in (
                ("Auto", DisplayRotation.AUTO),
                ("0°", DisplayRotation.DEG_0),
                ("90°", DisplayRotation.DEG_90),
                ("180°", DisplayRotation.DEG_180),
                ("270°", DisplayRotation.DEG_270),
            ):
                rotation_box.addItem(label, value)
            rotation_box.setToolTip(
                "Physical monitor rotation; Auto follows the OS display orientation."
            )
            rotation_box.setMinimumWidth(_s(72))

            default_rows, default_cols = self._grid_defaults[role]
            saved_layout = (
                dict(saved_settings)
                if self._using_stable_settings
                else dict(self._saved_display_layouts.get(s.name(), {}))
            )
            rows, cols = self._saved_grid_for(s, role)
            display_layout = normalize_display_layout({
                "rotation": saved_layout.get("rotation", DisplayRotation.AUTO),
                "rows": rows,
                "cols": cols,
            })
            rotation_index = rotation_box.findData(display_layout["rotation"])
            rotation_box.setCurrentIndex(max(0, rotation_index))
            self._rotation_boxes[label_text] = rotation_box
            rotation_box.currentIndexChanged.connect(
                lambda _index, item=item: item.setSelected(True)
            )
            row_layout.addWidget(rotation_box)

            grid_box = QComboBox()
            for grid_rows in range(1, 7):
                for grid_cols in range(1, 7):
                    grid_box.addItem(
                        f"{grid_rows} × {grid_cols}",
                        (grid_rows, grid_cols),
                    )
            grid_box.setToolTip("Videos per display: rows × columns.")
            grid_box.setMinimumWidth(_s(74))
            grid_index = grid_box.findData(
                (display_layout["rows"], display_layout["cols"])
            )
            grid_box.setCurrentIndex(max(0, grid_index))
            self._grid_boxes[label_text] = grid_box
            grid_box.currentIndexChanged.connect(
                lambda _index, item=item, label=label_text: (
                    item.setSelected(True),
                    self.list_disp.setCurrentItem(item),
                    self._remember_grid_selection(label),
                    self._sync_selected_preview()
                )
            )
            row_layout.addWidget(grid_box)
            self.list_disp.setItemWidget(item, row)

            # Match selection state to the underlying item
            if (
                bool(saved_settings.get("selected", False))
                if self._using_stable_settings
                else s.name() in prev_screens
            ):
                item.setSelected(True)
            # Size the row to its contents
            item.setSizeHint(row.sizeHint())

        ld.addWidget(self.list_disp)
        self.list_disp.currentItemChanged.connect(
            lambda _current, _previous: self._sync_selected_preview()
        )
        panels.addWidget(grp_disp)

        # ── Sources ──
        grp_lib = QGroupBox("SOURCES")
        ll = QVBoxLayout(grp_lib)
        self.list_lib = QListWidget()
        self.list_lib.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection
        )
        prev_libs = last_libraries.split(",") if last_libraries else []

        for lib in libraries:
            item = QListWidgetItem(lib)
            self.list_lib.addItem(item)
            if lib in prev_libs:
                item.setSelected(True)

        ll.addWidget(self.list_lib)
        panels.addWidget(grp_lib)

        layout.addLayout(panels)

        # ── Live preview ──
        grp_preview = QGroupBox("LIVE PREVIEW · SELECTED MONITOR")
        pg = QHBoxLayout(grp_preview)
        pg.setSpacing(_s(12))
        self.preview = _GridPreview()
        pg.addWidget(self.preview)
        self.lbl_preview = QLabel()
        self.lbl_preview.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {_s(11)}px;"
            " background: transparent;"
        )
        pg.addWidget(self.lbl_preview)
        pg.addStretch()
        layout.addWidget(grp_preview)

        btn = QPushButton("▶   INITIALIZE SYSTEM")
        btn.clicked.connect(self.accept)
        btn.setDefault(True)  # Enter starts the wall
        btn.setAutoDefault(True)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)

        if screens:
            initial_index = 0
            if not self._using_stable_settings:
                initial_index = next(
                    (
                        index for index, screen in enumerate(screens)
                        if screen.name() in prev_screens
                    ),
                    0,
                )
            self.list_disp.setCurrentRow(initial_index)
        self._sync_selected_preview()

    def _remember_grid_selection(self, label: str) -> None:
        """Remember the most recent valid grid selected for this role."""
        role = self._role_boxes[label].currentData()
        value = self._grid_boxes[label].currentData()
        self._last_selected_grids = update_last_selected_grid(
            self._last_selected_grids, role, value
        )

    def _role_changed(self, label: str) -> None:
        """Switch a monitor to its role default without losing its rotation."""
        role = self._role_boxes[label].currentData()
        saved_settings = (
            restore_display_settings(
                self._screen_map[label],
                self._saved_display_settings,
                wall_grid=self._grid_defaults[DisplayRole.WALL],
                preview_grid=self._grid_defaults[DisplayRole.PREVIEW],
            )
            if self._using_stable_settings
            else {}
        )
        saved_role = saved_settings.get("role")
        if not self._using_stable_settings:
            saved_role = self._saved_display_roles.get(self._screen_map[label].name())
        rows, cols = grid_for_role_switch(
            self._last_selected_grids,
            self._grid_defaults,
            saved_role,
            role,
            *self._saved_grid_for(self._screen_map[label], role),
        )
        box = self._grid_boxes[label]
        index = box.findData((rows, cols))
        if index >= 0:
            box.setCurrentIndex(index)

    def _saved_grid_for(
        self, screen: Any, role: str,
    ) -> tuple[int, int]:
        """Best saved grid for a display: identity → name-keyed → role default.

        Stable identity is authoritative, but the pure-fallback identity
        (no serial/connector/EDID) embeds screen geometry, which can drift
        between launches — so an identity miss falls back to the always-
        written name-keyed layout map instead of the role default.
        """
        default_rows, default_cols = self._grid_defaults[role]
        identity_hit = (
            self._saved_display_settings.get(display_identity(screen))
            if self._using_stable_settings
            else None
        )
        name_layout = self._saved_display_layouts.get(screen.name())
        if identity_hit is None and self._using_stable_settings:
            rows, cols = resolve_saved_grid(
                None, name_layout, (default_rows, default_cols),
            )
            if (rows, cols) != (default_rows, default_cols):
                logger.info(
                    "Display identity miss for %s — restoring "
                    "name-keyed grid %d×%d.",
                    screen.name(), rows, cols,
                )
            return rows, cols
        return resolve_saved_grid(
            dict(identity_hit) if isinstance(identity_hit, dict) else None,
            name_layout,
            (default_rows, default_cols),
        )

    def _sync_selected_preview(self) -> None:
        """Show the current monitor's selected role/grid in the live preview."""
        item = self.list_disp.currentItem()
        label = self._preview_labels_by_item.get(id(item)) if item else None
        if label is None and self._screen_map:
            label = next(iter(self._screen_map))
        if label is None:
            self.lbl_preview.setText("No monitors detected")
            return

        value = self._grid_boxes[label].currentData()
        if isinstance(value, tuple) and len(value) == 2:
            rows, cols = int(value[0]), int(value[1])
            role = self._role_boxes[label].currentData()
            role_name = "Preview" if role == DisplayRole.PREVIEW else "Wall"
            screen = self._screen_map[label]
            self.preview.set_grid(rows, cols)
            self.lbl_preview.setText(
                f"{screen.name()} · {role_name} · {rows} × {cols} "
                f"({rows * cols} cells)"
            )

    def get_settings(self) -> dict[str, Any]:
        """Return the selected configuration."""
        selected_labels = [
            label
            for label, item in self._screen_items.items()
            if item.isSelected()
        ]
        return {
            "screens": [self._screen_map[l] for l in selected_labels],
            "libraries": [
                i.text() for i in self.list_lib.selectedItems()
            ],
            "grid_rows": self._grid_for_role(DisplayRole.WALL)[0],
            "grid_cols": self._grid_for_role(DisplayRole.WALL)[1],
            "preview_rows": self._grid_for_role(DisplayRole.PREVIEW)[0],
            "preview_cols": self._grid_for_role(DisplayRole.PREVIEW)[1],
            "display_roles": {
                self._screen_map[l].name(): self._role_boxes[l].currentData()
                for l in self._screen_map
            },
            "display_layouts": {
                self._screen_map[l].name(): {
                    "rotation": self._rotation_boxes[l].currentData(),
                    "rows": self._grid_boxes[l].currentData()[0],
                    "cols": self._grid_boxes[l].currentData()[1],
                }
                for l in self._screen_map
            },
            "display_settings": {
                **self._saved_display_settings,
                **{
                    display_identity(self._screen_map[l]): {
                        "selected": self._screen_items[l].isSelected(),
                        "role": self._role_boxes[l].currentData(),
                        "rotation": self._rotation_boxes[l].currentData(),
                        "rows": self._grid_boxes[l].currentData()[0],
                        "cols": self._grid_boxes[l].currentData()[1],
                    }
                    for l in self._screen_map
                },
            },
        }

    def _grid_for_role(self, role: str) -> tuple[int, int]:
        """Return the most recently selected grid for a role."""
        remembered = self._last_selected_grids.get(role)
        if isinstance(remembered, tuple) and len(remembered) == 2:
            return int(remembered[0]), int(remembered[1])
        fallback = self._grid_defaults.get(role, (2, 2))
        for label, box in self._role_boxes.items():
            if box.currentData() == role:
                value = self._grid_boxes[label].currentData()
                if isinstance(value, tuple) and len(value) == 2:
                    return int(value[0]), int(value[1])
        return fallback
