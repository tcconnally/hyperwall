"""
Hyperwall — SetupWizard.

Monitor + library + grid layout selection dialog.
"""

from __future__ import annotations

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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import VERSION_SHORT
from . import theme
from .constants import (
    DisplayRole,
    DisplayRotation,
    _s,
    normalize_display_layout,
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
    """Pre-launch configuration dialog: select monitors, libraries, grid."""

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
    ):
        super().__init__()
        self.setWindowTitle(f"HyperWall {VERSION_SHORT}")
        self.resize(_s(1_020), _s(620))
        self.setStyleSheet(theme.dialog_qss())

        self._screen_map: dict[str, Any] = {}
        self._screen_items: dict[str, QListWidgetItem] = {}
        self._role_boxes: dict[str, QComboBox] = {}
        self._rotation_boxes: dict[str, QComboBox] = {}
        self._grid_boxes: dict[str, QComboBox] = {}
        last_display_roles = last_display_roles or {}
        last_display_layouts = last_display_layouts or {}

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
        grp_disp = QGroupBox("DISPLAYS · ROLE / ROTATION / VIDEO GRID")
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

            row = _DisplayRow()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(_s(6), _s(2), _s(6), _s(2))
            row_layout.setSpacing(_s(8))
            row.clicked.connect(lambda item=item: item.setSelected(True))
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
            role = last_display_roles.get(s.name(), DisplayRole.WALL)
            role_box.setCurrentIndex(
                0 if role == DisplayRole.WALL else 1
            )
            self._role_boxes[label_text] = role_box
            role_box.currentIndexChanged.connect(
                lambda _index, item=item: item.setSelected(True)
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

            default_rows = last_preview_rows if role == DisplayRole.PREVIEW else last_rows
            default_cols = last_preview_cols if role == DisplayRole.PREVIEW else last_cols
            saved_layout = last_display_layouts.get(s.name(), {})
            display_layout = normalize_display_layout({
                "rotation": saved_layout.get("rotation", DisplayRotation.AUTO),
                "rows": saved_layout.get("rows", default_rows),
                "cols": saved_layout.get("cols", default_cols),
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
                lambda _index, item=item: item.setSelected(True)
            )
            row_layout.addWidget(grid_box)
            self.list_disp.setItemWidget(item, row)

            # Match selection state to the underlying item
            if s.name() in prev_screens:
                item.setSelected(True)
            # Size the row to its contents
            item.setSizeHint(row.sizeHint())

        ld.addWidget(self.list_disp)
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

        # ── Wall grid + live preview ──
        grp_wall = QGroupBox("FALLBACK WALL GRID (new displays)")
        lg = QHBoxLayout(grp_wall)
        lg.setSpacing(_s(12))
        self.rows = QSpinBox()
        self.rows.setRange(1, 6)
        self.rows.setValue(last_rows)
        self.cols = QSpinBox()
        self.cols.setRange(1, 6)
        self.cols.setValue(last_cols)
        lg.addWidget(QLabel("ROWS"))
        lg.addWidget(self.rows)
        lg.addSpacing(_s(12))
        lg.addWidget(QLabel("COLS"))
        lg.addWidget(self.cols)
        lg.addSpacing(_s(16))

        self.preview = _GridPreview(last_rows, last_cols)
        lg.addWidget(self.preview)
        self.lbl_cells = QLabel()
        self.lbl_cells.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {_s(11)}px; background: transparent;"
        )
        lg.addWidget(self.lbl_cells)
        lg.addStretch()
        layout.addWidget(grp_wall)

        # ── Preview grid ──
        grp_preview = QGroupBox("FALLBACK PREVIEW GRID (new displays)")
        pg = QHBoxLayout(grp_preview)
        pg.setSpacing(_s(12))
        self.preview_rows = QSpinBox()
        self.preview_rows.setRange(1, 6)
        self.preview_rows.setValue(last_preview_rows)
        self.preview_cols = QSpinBox()
        self.preview_cols.setRange(1, 6)
        self.preview_cols.setValue(last_preview_cols)
        pg.addWidget(QLabel("ROWS"))
        pg.addWidget(self.preview_rows)
        pg.addSpacing(_s(12))
        pg.addWidget(QLabel("COLS"))
        pg.addWidget(self.preview_cols)
        pg.addSpacing(_s(16))

        self.preview_preview = _GridPreview(last_preview_rows, last_preview_cols)
        pg.addWidget(self.preview_preview)
        self.lbl_preview_cells = QLabel()
        self.lbl_preview_cells.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: {_s(11)}px; background: transparent;"
        )
        pg.addWidget(self.lbl_preview_cells)
        pg.addStretch()
        layout.addWidget(grp_preview)

        btn = QPushButton("▶   INITIALIZE SYSTEM")
        btn.clicked.connect(self.accept)
        btn.setDefault(True)  # Enter starts the wall
        btn.setAutoDefault(True)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)

        self.rows.valueChanged.connect(self._sync_preview)
        self.cols.valueChanged.connect(self._sync_preview)
        self.preview_rows.valueChanged.connect(self._sync_preview)
        self.preview_cols.valueChanged.connect(self._sync_preview)
        self._sync_preview()

    def _sync_preview(self) -> None:
        r, c = self.rows.value(), self.cols.value()
        self.preview.set_grid(r, c)
        self.lbl_cells.setText(f"{r * c} cells / display")
        pr, pc = self.preview_rows.value(), self.preview_cols.value()
        self.preview_preview.set_grid(pr, pc)
        self.lbl_preview_cells.setText(f"{pr * pc} cells / display")

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
            "grid_rows": self.rows.value(),
            "grid_cols": self.cols.value(),
            "preview_rows": self.preview_rows.value(),
            "preview_cols": self.preview_cols.value(),
            "display_roles": {
                self._screen_map[l].name(): self._role_boxes[l].currentData()
                for l in selected_labels
            },
            "display_layouts": {
                self._screen_map[l].name(): {
                    "rotation": self._rotation_boxes[l].currentData(),
                    "rows": self._grid_boxes[l].currentData()[0],
                    "cols": self._grid_boxes[l].currentData()[1],
                }
                for l in self._screen_map
            },
        }
