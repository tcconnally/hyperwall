from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QListWidget, QListWidgetItem, QSpinBox, QPushButton
)

class SetupWizard(QDialog):
    def __init__(self, config, screens, libraries: list[str]):
        super().__init__()
        self.setWindowTitle("HyperWall 8.0")
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20); layout.setSpacing(14)

        title = QLabel("HYPERWALL  8.0")
        title.setStyleSheet("font-size: 24px; font-weight: 900; color: white; letter-spacing: 3px;")
        layout.addWidget(title)

        panels = QHBoxLayout(); panels.setSpacing(14)

        grp_disp = QGroupBox("DISPLAYS")
        l_disp = QVBoxLayout(grp_disp)
        self.list_disp = QListWidget()
        self.list_disp.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self._screen_map: dict[str, object] = {}
        last_screens = config.get("Settings", "last_screens", fallback="").split(",")
        for idx, s in enumerate(screens, 1):
            label = f"Monitor {idx} — {s.name()}  [{s.geometry().width()}×{s.geometry().height()}]"
            item = QListWidgetItem(label); self.list_disp.addItem(item)
            self._screen_map[label] = s
            if s.name() in last_screens:
                item.setSelected(True)
        l_disp.addWidget(self.list_disp); panels.addWidget(grp_disp)

        grp_lib = QGroupBox("SOURCES")
        l_lib = QVBoxLayout(grp_lib)
        self.list_lib = QListWidget()
        self.list_lib.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        last_libs = config.get("Settings", "last_libraries", fallback="").split(",")
        for lib in libraries:
            item = QListWidgetItem(lib); self.list_lib.addItem(item)
            if lib in last_libs:
                item.setSelected(True)
        l_lib.addWidget(self.list_lib); panels.addWidget(grp_lib)

        layout.addLayout(panels)

        grp_grid = QGroupBox("LAYOUT")
        l_grid = QHBoxLayout(grp_grid)
        self.rows = QSpinBox(); self.rows.setRange(1, 6)
        self.rows.setValue(int(config.get("Settings", "last_grid_rows", fallback="2")))
        self.cols = QSpinBox(); self.cols.setRange(1, 6)
        self.cols.setValue(int(config.get("Settings", "last_grid_cols", fallback="2")))
        l_grid.addWidget(QLabel("ROWS")); l_grid.addWidget(self.rows)
        l_grid.addSpacing(20)
        l_grid.addWidget(QLabel("COLS")); l_grid.addWidget(self.cols)
        l_grid.addStretch()
        btn = QPushButton("▶   INITIALIZE SYSTEM"); btn.clicked.connect(self.accept)
        l_grid.addWidget(btn)
        layout.addWidget(grp_grid)

    def get_settings(self) -> dict:
        return {
            "screens":   [self._screen_map[i.text()] for i in self.list_disp.selectedItems()],
            "libraries": [i.text() for i in self.list_lib.selectedItems()],
            "grid":      (self.rows.value(), self.cols.value()),
        }
