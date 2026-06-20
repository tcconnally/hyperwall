"""
Hyperwall v9 — SetupWizard.

Monitor + library + grid layout selection dialog.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
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
)


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
        self.setWindowTitle("HyperWall 9.0")
        self.resize(720, 540)
        self.setStyleSheet("""
            QDialog { background: #0e0e0e; color: #eee; font-family: 'Segoe UI'; }
            QGroupBox {
                border: 1px solid #2a2a2a; border-radius: 4px; margin-top: 8px;
                font-weight: bold; font-size: 11px; color: #3b8edb; background: #141414;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
            QListWidget { background: #181818; border: 1px solid #2a2a2a; color: #ccc; outline: none; }
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

        self._screen_map: dict[str, Any] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        title = QLabel("HYPERWALL  9.0")
        title.setStyleSheet(
            "font-size: 24px; font-weight: 900; color: white; letter-spacing: 3px;"
        )
        layout.addWidget(title)

        panels = QHBoxLayout()
        panels.setSpacing(14)

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

        # ── Grid ──
        grp_grid = QGroupBox("LAYOUT")
        lg = QHBoxLayout(grp_grid)
        self.rows = QSpinBox()
        self.rows.setRange(1, 6)
        self.rows.setValue(last_rows)
        self.cols = QSpinBox()
        self.cols.setRange(1, 6)
        self.cols.setValue(last_cols)
        lg.addWidget(QLabel("ROWS"))
        lg.addWidget(self.rows)
        lg.addSpacing(20)
        lg.addWidget(QLabel("COLS"))
        lg.addWidget(self.cols)
        lg.addStretch()
        btn = QPushButton("▶   INITIALIZE SYSTEM")
        btn.clicked.connect(self.accept)
        lg.addWidget(btn)
        layout.addWidget(grp_grid)

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
