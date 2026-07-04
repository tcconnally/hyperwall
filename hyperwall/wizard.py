"""
Hyperwall — SetupWizard.

Monitor + library + grid layout selection dialog.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
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
from .constants import UI_SCALE


def _s(px: int) -> int:
    return max(1, int(px * UI_SCALE))


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
    ):
        super().__init__()
        self.setWindowTitle(f"HyperWall {VERSION_SHORT}")
        self.resize(_s(760), _s(560))
        self.setStyleSheet(theme.dialog_qss())

        self._screen_map: dict[str, Any] = {}

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
        grp_disp = QGroupBox("DISPLAYS")
        ld = QVBoxLayout(grp_disp)
        self.list_disp = QListWidget()
        self.list_disp.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        prev_screens = last_screens.split(",") if last_screens else []

        for idx, s in enumerate(screens, 1):
            label = (
                f"Monitor {idx} — {s.name()}  "
                f"[{s.geometry().width()}x{s.geometry().height()}]"
            )
            item = QListWidgetItem(label)
            self.list_disp.addItem(item)
            self._screen_map[label] = s
            if s.name() in prev_screens:
                item.setSelected(True)

        ld.addWidget(self.list_disp)
        panels.addWidget(grp_disp)

        # ── Sources ──
        grp_lib = QGroupBox("SOURCES")
        ll = QVBoxLayout(grp_lib)
        self.list_lib = QListWidget()
        self.list_lib.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        prev_libs = last_libraries.split(",") if last_libraries else []

        for lib in libraries:
            item = QListWidgetItem(lib)
            self.list_lib.addItem(item)
            if lib in prev_libs:
                item.setSelected(True)

        ll.addWidget(self.list_lib)
        panels.addWidget(grp_lib)

        layout.addLayout(panels)

        # ── Grid + live preview ──
        grp_grid = QGroupBox("LAYOUT")
        lg = QHBoxLayout(grp_grid)
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

        btn = QPushButton("▶   INITIALIZE SYSTEM")
        btn.clicked.connect(self.accept)
        btn.setDefault(True)  # Enter starts the wall
        btn.setAutoDefault(True)
        lg.addWidget(btn)
        layout.addWidget(grp_grid)

        self.rows.valueChanged.connect(self._sync_preview)
        self.cols.valueChanged.connect(self._sync_preview)
        self._sync_preview()

    def _sync_preview(self) -> None:
        r, c = self.rows.value(), self.cols.value()
        self.preview.set_grid(r, c)
        self.lbl_cells.setText(f"{r * c} cells / display")

    def get_settings(self) -> dict[str, Any]:
        """Return the selected configuration."""
        return {
            "screens": [
                self._screen_map[i.text()]
                for i in self.list_disp.selectedItems()
            ],
            "libraries": [
                i.text() for i in self.list_lib.selectedItems()
            ],
            "grid_rows": self.rows.value(),
            "grid_cols": self.cols.value(),
        }
