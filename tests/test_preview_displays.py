"""Tests for the preview-display / solo-fullscreen feature.

Pure-logic tests run everywhere. Qt construction tests follow the project
convention and only run on Windows (where PyQt6 + offscreen are available).
"""
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    _PYQT = os.name == "nt"
except ImportError:
    _PYQT = False

from hyperwall.config import HyperwallConfig
from hyperwall.constants import DisplayRole


# ── constants / config ──

def test_wizard_uses_qt_item_view_selection_enum():
    wizard_source = (
        Path(__file__).resolve().parents[1] / "hyperwall" / "wizard.py"
    ).read_text(encoding="utf-8")
    assert "QAbstractItemView.SelectionMode.MultiSelection" in wizard_source
    assert "QListWidgetItem.SelectionMode" not in wizard_source

def test_display_role_values():
    assert DisplayRole.WALL == "wall"
    assert DisplayRole.PREVIEW == "preview"
    assert DisplayRole.is_valid("wall") is True
    assert DisplayRole.is_valid("preview") is True
    assert DisplayRole.is_valid("bogus") is False
    assert DisplayRole.is_valid(None) is False


def test_config_preview_fields_round_trip():
    cfg = HyperwallConfig(
        server_url="http://localhost:8096",
        username="u",
        password="p",
        last_grid_rows=2,
        last_grid_cols=2,
        last_preview_rows=3,
        last_preview_cols=4,
        last_display_roles=json.dumps({"HDMI-1": "preview"}),
    )
    assert cfg.last_preview_rows == 3
    assert cfg.last_preview_cols == 4
    assert cfg.display_roles() == {"HDMI-1": "preview"}


def test_config_malformed_display_roles_returns_empty():
    cfg = HyperwallConfig(
        server_url="http://localhost:8096",
        username="u",
        password="p",
        last_display_roles="not-json",
    )
    assert cfg.display_roles() == {}


def test_display_layout_defaults_and_rotation_values():
    from hyperwall.constants import DisplayRotation, normalize_display_layout

    assert DisplayRotation.AUTO == "auto"
    assert DisplayRotation.DEG_90 == "90"
    assert DisplayRotation.DEG_270 == "270"
    assert normalize_display_layout({}) == {
        "rotation": "auto", "rows": 2, "cols": 2,
    }
    assert normalize_display_layout(
        {"rotation": "90", "rows": 4, "cols": 3}
    ) == {"rotation": "90", "rows": 4, "cols": 3}


def test_config_save_load_preview_fields():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.ini")
        cfg = HyperwallConfig(
            server_url="http://localhost:8096",
            username="u",
            password="p",
            last_preview_rows=4,
            last_preview_cols=5,
            last_display_roles=json.dumps({"DP-1": "wall", "eDP-1": "preview"}),
        )
        cfg.save(path)
        loaded = HyperwallConfig.load(path)
        assert loaded.last_preview_rows == 4
        assert loaded.last_preview_cols == 5
        assert loaded.display_roles() == {"DP-1": "wall", "eDP-1": "preview"}


# ── WallController construction (Windows/Qt only) ──

def test_wall_controller_builds_wall_and_preview_windows():
    if not _PYQT:
        raise AssertionError("SKIP")

    from PyQt6.QtCore import QRect
    from hyperwall.wall import WallController
    from hyperwall.emby import EmbyClient

    class _FakeClient:
        def __init__(self):
            self.access_token = "token"
            self.server_url = "http://localhost"
            self.user_id = "uid"
            self.backend = type("B", (), {"requires_static_true": True})()

        def test_connection(self):
            return True

        def authenticate(self):
            return True

        def fetch_libraries(self):
            return []

        def close(self):
            pass

        def get(self, *a, **k):
            return type("R", (), {"json": lambda: {}})()

        def post(self, *a, **k):
            return type("R", (), {"status_code": 200})()

    class _FakeScreen:
        def __init__(self, name, x, y, w, h):
            self._name = name
            self._geo = QRect(x, y, w, h)

        def name(self):
            return self._name

        def geometry(self):
            return self._geo

    screens = [
        _FakeScreen("External", 0, 0, 2560, 1440),
        _FakeScreen("Laptop", 2560, 0, 1920, 1080),
    ]
    display_roles = {"External": DisplayRole.WALL, "Laptop": DisplayRole.PREVIEW}
    display_layouts = {
        "External": {"rotation": "90", "rows": 3, "cols": 2},
        "Laptop": {"rotation": "0", "rows": 2, "cols": 3},
    }

    wall = WallController(
        screens=screens,
        libraries=["Movies"],
        grid_rows=2,
        grid_cols=2,
        client=_FakeClient(),
        display_roles=display_roles,
        display_layouts=display_layouts,
        preview_rows=3,
        preview_cols=4,
    )

    # One wall window (2x2 = 4 cells) + one preview window (3x4 = 12 cells)
    assert len(wall.windows) == 2
    assert len(wall.cells) == 16
    meta = wall._window_meta
    roles = {m["role"] for m in meta.values()}
    assert roles == {DisplayRole.WALL, DisplayRole.PREVIEW}
    by_name = {
        meta["screen"].name(): meta for meta in meta.values()
    }
    assert (by_name["External"]["rows"], by_name["External"]["cols"]) == (3, 2)
    assert by_name["External"]["rotation"] == "90"
    assert (by_name["Laptop"]["rows"], by_name["Laptop"]["cols"]) == (2, 3)

    wall._cleanup()


def test_solo_mode_round_trip():
    if not _PYQT:
        raise AssertionError("SKIP")

    from PyQt6.QtCore import QRect
    from hyperwall.wall import WallController

    class _FakeClient:
        access_token = "token"
        server_url = "http://localhost"
        user_id = "uid"
        backend = type("B", (), {"requires_static_true": True})()

        def test_connection(self):
            return True

        def authenticate(self):
            return True

        def fetch_libraries(self):
            return []

        def close(self):
            pass

        def get(self, *a, **k):
            return type("R", (), {"json": lambda: {}})()

        def post(self, *a, **k):
            return type("R", (), {"status_code": 200})()

    class _FakeScreen:
        def __init__(self, name, x, y, w, h):
            self._name = name
            self._geo = QRect(x, y, w, h)

        def name(self):
            return self._name

        def geometry(self):
            return self._geo

    wall = WallController(
        screens=[_FakeScreen("Preview", 0, 0, 1920, 1080)],
        libraries=["Movies"],
        grid_rows=2,
        grid_cols=2,
        client=_FakeClient(),
        display_roles={"Preview": DisplayRole.PREVIEW},
    )

    cell = wall.cells[0]
    assert wall._solo_cell is None
    wall._enter_solo(cell)
    assert wall._solo_cell is cell
    assert wall._window_meta[id(wall.windows[0])]["solo"] is True
    wall._exit_solo()
    assert wall._solo_cell is None
    assert wall._window_meta[id(wall.windows[0])]["solo"] is False

    wall._cleanup()


# ── runner ──

def run_all() -> int:
    failures = 0
    for name, fn in globals().items():
        if not name.startswith("test_"):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            if str(e) == "SKIP":
                print(f"  SKIP  {name}")
            else:
                print(f"  FAIL  {name}: {e}")
                failures += 1
        except Exception as e:
            print(f"  FAIL  {name}: {e}")
            failures += 1
    print(f"\n{failures} failed out of {sum(1 for n in globals() if n.startswith('test_'))} tests.")
    return failures


if __name__ == "__main__":
    sys.exit(run_all())
