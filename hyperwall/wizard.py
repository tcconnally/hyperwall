from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QListWidget, QListWidgetItem, QSpinBox, QPushButton
)

from .style import CYAN, MAGENTA, BG_DEEP, BG_SURFACE, BG_RAISED, TEXT, TEXT_DIM, BORDER, BORDER_GLOW

WIZARD_QSS = f"""
    QDialog {{
        background-color: {BG_DEEP};
        color: {TEXT};
    }}
    QGroupBox {{
        border: 1px solid {BORDER};
        border-radius: 6px;
        margin-top: 14px;
        padding-top: 12px;
        background-color: {BG_SURFACE};
        font-weight: 900;
        font-size: 11px;
        color: {CYAN};
        letter-spacing: 2px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 8px;
        background-color: {BG_DEEP};
    }}
    QListWidget {{
        background-color: {BG_RAISED};
        border: 1px solid {BORDER};
        color: {TEXT};
        outline: none;
        border-radius: 4px;
        padding: 3px;
        font-size: 12px;
    }}
    QListWidget::item {{
        padding: 5px 10px;
        border-radius: 2px;
        margin: 1px 0;
    }}
    QListWidget::item:selected {{
        background-color: {CYAN};
        color: {BG_DEEP};
        font-weight: 700;
    }}
    QListWidget::item:hover:!selected {{
        background-color: {BG_SURFACE};
        border: 1px solid {BORDER_GLOW};
    }}
    QSpinBox {{
        background-color: {BG_RAISED};
        color: {TEXT};
        border: 1px solid {BORDER};
        padding: 6px 10px;
        min-width: 64px;
        border-radius: 4px;
        font-size: 14px;
        font-weight: 700;
        font-family: "Consolas", "Cascadia Code", monospace;
    }}
    QSpinBox:focus {{
        border-color: {CYAN};
    }}
    QSpinBox::up-button, QSpinBox::down-button {{
        width: 18px;
        border-radius: 2px;
    }}
    QPushButton {{
        background-color: #0d2847;
        color: {CYAN};
        border: 2px solid {CYAN};
        padding: 12px 28px;
        font-weight: 900;
        font-size: 13px;
        border-radius: 5px;
        letter-spacing: 2px;
        text-transform: uppercase;
        min-width: 0; min-height: 0;
        max-width: 9999px; max-height: 9999px;
    }}
    QPushButton:hover {{
        background-color: {CYAN};
        color: {BG_DEEP};
    }}
    QPushButton:pressed {{
        background-color: #009faf;
        border-color: #009faf;
    }}
    QLabel {{
        color: {TEXT_DIM};
        background: transparent;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 1px;
    }}
"""

class SetupWizard(QDialog):
    def __init__(self, config, screens, libraries: list[str]):
        super().__init__()
        self.setWindowTitle("HYPERWALL  —  INITIALIZE")
        self.resize(760, 580)
        self.setStyleSheet(WIZARD_QSS)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 26)
        layout.setSpacing(16)

        # ── Header ──────────────────────────────────────────────────────
        hdr = QHBoxLayout()
        title = QLabel("◈  HYPERWALL  <span style='color:#fff;'>8.2</span>")
        title.setStyleSheet(
            f"font-size: 26px; font-weight: 900; color: {CYAN};"
            " letter-spacing: 3px; background: transparent;"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        hdr.addWidget(title)
        hdr.addStretch()

        sub = QLabel("MULTI-MONITOR  VIDEO  MATRIX")
        sub.setStyleSheet(
            f"color: {TEXT_DIM}; font-size: 11px; font-weight: 600;"
            " letter-spacing: 4px; background: transparent;"
        )
        hdr.addWidget(sub)
        layout.addLayout(hdr)

        # ── Separator ───────────────────────────────────────────────────
        sep = QLabel()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {BORDER}; border: none;")
        layout.addWidget(sep)

        # ── Display + Source panels ─────────────────────────────────────
        panels = QHBoxLayout()
        panels.setSpacing(16)

        grp_disp = QGroupBox("◈  DISPLAYS")
        l_disp = QVBoxLayout(grp_disp)
        l_disp.setContentsMargins(10, 16, 10, 10)
        self.list_disp = QListWidget()
        self.list_disp.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self._screen_map: dict[str, object] = {}
        last_screens = config.get("Settings", "last_screens", fallback="").split(",")
        for idx, s in enumerate(screens, 1):
            label = (
                f"MON {idx}  —  {s.name()}"
                f"  [{s.geometry().width()}×{s.geometry().height()}]"
            )
            item = QListWidgetItem(label)
            self.list_disp.addItem(item)
            self._screen_map[label] = s
            if s.name() in last_screens:
                item.setSelected(True)
        l_disp.addWidget(self.list_disp)
        panels.addWidget(grp_disp)

        grp_lib = QGroupBox("◈  SOURCES")
        l_lib = QVBoxLayout(grp_lib)
        l_lib.setContentsMargins(10, 16, 10, 10)
        self.list_lib = QListWidget()
        self.list_lib.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        last_libs = config.get("Settings", "last_libraries", fallback="").split(",")
        for lib in libraries:
            item = QListWidgetItem(f"▸ {lib}")
            self.list_lib.addItem(item)
            if lib in last_libs:
                item.setSelected(True)
        l_lib.addWidget(self.list_lib)
        panels.addWidget(grp_lib)

        layout.addLayout(panels)

        # ── Layout + Launch ─────────────────────────────────────────────
        grp_grid = QGroupBox("◈  LAYOUT")
        l_grid = QHBoxLayout(grp_grid)
        l_grid.setContentsMargins(14, 18, 14, 14)

        self.rows = QSpinBox()
        self.rows.setRange(1, 6)
        self.rows.setValue(int(config.get("Settings", "last_grid_rows", fallback="2")))
        self.cols = QSpinBox()
        self.cols.setRange(1, 6)
        self.cols.setValue(int(config.get("Settings", "last_grid_cols", fallback="2")))

        l_grid.addWidget(QLabel("ROWS"))
        l_grid.addWidget(self.rows)
        l_grid.addSpacing(24)
        l_grid.addWidget(QLabel("COLS"))
        l_grid.addWidget(self.cols)
        l_grid.addStretch()

        btn = QPushButton("▶   INITIALIZE  SYSTEM")
        btn.clicked.connect(self.accept)
        l_grid.addWidget(btn)
        layout.addWidget(grp_grid)

    def get_settings(self) -> dict:
        return {
            "screens":   [self._screen_map[i.text()] for i in self.list_disp.selectedItems()],
            "libraries": [i.text().replace("▸ ", "") for i in self.list_lib.selectedItems()],
            "grid":      (self.rows.value(), self.cols.value()),
        }
