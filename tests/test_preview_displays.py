"""Tests for the preview-display / solo-fullscreen feature.

Pure-logic tests run everywhere. Wizard Qt tests run wherever PyQt6 +
offscreen are available; wall/solo construction remains Windows-only.
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
    _HAS_PYQT = True
    _PYQT = os.name == "nt"
except (ImportError, RuntimeError):
    _HAS_PYQT = False
    _PYQT = False

from hyperwall.config import HyperwallConfig
from hyperwall.constants import DisplayRole
from hyperwall.wizard_logic import (
    grid_for_role_switch,
    grid_index_for_value,
    normalize_grid_value,
    resolve_saved_grid,
    update_last_selected_grid,
)


# ── constants / config ──

def test_last_selected_grid_becomes_default_for_that_role():
    remembered = {
        DisplayRole.WALL: (2, 2),
        DisplayRole.PREVIEW: (3, 4),
    }

    updated = update_last_selected_grid(
        remembered, DisplayRole.WALL, (5, 6)
    )

    assert updated == {
        DisplayRole.WALL: (5, 6),
        DisplayRole.PREVIEW: (3, 4),
    }


def test_invalid_last_selected_grid_does_not_replace_default():
    remembered = {
        DisplayRole.WALL: (2, 2),
        DisplayRole.PREVIEW: (3, 4),
    }

    updated = update_last_selected_grid(
        remembered, DisplayRole.WALL, (0, 7)
    )

    assert updated == remembered


def test_grid_index_matches_qt_sequence_shape():
    assert grid_index_for_value([[1, 1], [2, 2], [3, 4]], (2, 2)) == 1


def test_current_monitor_selection_restores_by_stable_identity():
    from hyperwall.wizard_logic import initial_display_index

    identities = ["screen-a", "screen-b"]
    settings = {
        "screen-a": {"selected": True, "current": False},
        "screen-b": {"selected": True, "current": True},
    }

    assert initial_display_index(identities, settings) == 1


def test_current_monitor_selection_falls_back_to_first_selected_identity():
    from hyperwall.wizard_logic import initial_display_index

    identities = ["screen-a", "screen-b"]
    settings = {
        "screen-a": {"selected": False},
        "screen-b": {"selected": True},
    }

    assert initial_display_index(identities, settings) == 1

def test_wizard_wires_grid_changes_to_last_selected_defaults():
    wizard_source = (
        Path(__file__).resolve().parents[1] / "hyperwall" / "wizard.py"
    ).read_text(encoding="utf-8")
    assert "self._remember_grid_selection(label)" in wizard_source
    assert "update_last_selected_grid" in wizard_source
    assert "self._last_selected_grids" in wizard_source

def test_wizard_uses_qt_safe_grid_index_lookup():
    wizard_source = (
        Path(__file__).resolve().parents[1] / "hyperwall" / "wizard.py"
    ).read_text(encoding="utf-8")
    assert "self._grid_index_for(" in wizard_source
    assert "grid_box.findData((" not in wizard_source
    assert "currentData()[0]" not in wizard_source
    assert "currentData()[1]" not in wizard_source


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


def test_wizard_uses_per_monitor_role_and_grid_as_preview_source():
    wizard = Path(__file__).resolve().parents[1] / "hyperwall" / "wizard.py"
    source = wizard.read_text(encoding="utf-8")
    assert "display_layouts" in source
    assert "display_roles" in source
    assert "self._grid_boxes" in source
    assert "self.preview_rows" not in source
    assert "self.preview_cols" not in source
    assert "FALLBACK WALL GRID" not in source
    assert "FALLBACK PREVIEW GRID" not in source


def test_wizard_settings_persist_preview_role_and_grid_for_all_monitors():
    wizard = Path(__file__).resolve().parents[1] / "hyperwall" / "wizard.py"
    source = wizard.read_text(encoding="utf-8")
    assert 'for l in self._screen_map' in source
    assert 'self._role_boxes[l].currentData()' in source
    assert 'self._grid_for_label(l)[0]' in source
    assert 'self._grid_for_label(l)[1]' in source
    assert 'currentData()[0]' not in source
    assert 'currentData()[1]' not in source


def test_wizard_role_change_reloads_that_monitor_grid_preview():
    wizard = Path(__file__).resolve().parents[1] / "hyperwall" / "wizard.py"
    source = wizard.read_text(encoding="utf-8")
    assert "role_box.currentIndexChanged.connect" in source
    assert "grid_box.setCurrentIndex" in source
    assert "_sync_selected_preview" in source


def test_wizard_live_preview_follows_selected_monitor_and_grid():
    wizard = Path(__file__).resolve().parents[1] / "hyperwall" / "wizard.py"
    source = wizard.read_text(encoding="utf-8")
    assert 'QGroupBox("LIVE PREVIEW · SELECTED MONITOR")' in source
    assert "currentItemChanged.connect" in source
    assert "self.preview.set_grid(rows, cols)" in source
    assert "self._grid_boxes[label].currentData()" in source
    assert "self._sync_selected_preview()" in source
    assert "self.list_disp.setCurrentItem(item)" in source


def test_wizard_persists_roles_for_unselected_monitors():
    wizard = Path(__file__).resolve().parents[1] / "hyperwall" / "wizard.py"
    source = wizard.read_text(encoding="utf-8")
    roles_block = source[source.index('"display_roles"'):source.index('"display_layouts"')]
    assert 'for l in self._screen_map' in roles_block
    assert 'for l in selected_labels' not in roles_block


def test_wizard_uses_stable_identity_for_selection_and_settings_restore():
    wizard = Path(__file__).resolve().parents[1] / "hyperwall" / "wizard.py"
    source = wizard.read_text(encoding="utf-8")
    assert "display_identity(s)" in source
    assert "last_display_settings" in source
    assert "_using_stable_settings" in source
    assert '"selected": self._screen_items[l].isSelected()' in source


def test_wizard_restores_and_persists_current_monitor_selection():
    wizard = Path(__file__).resolve().parents[1] / "hyperwall" / "wizard.py"
    source = wizard.read_text(encoding="utf-8")
    assert "initial_display_index" in source
    assert '"current": self.list_disp.currentItem() is self._screen_items[l]' in source

def test_config_can_store_stable_display_settings():
    cfg = HyperwallConfig(
        server_url="http://localhost:8096",
        username="u",
        password="p",
        last_display_settings=json.dumps({
            "screen-v1:monitor-a": {
                "selected": True,
                "role": "preview",
                "rotation": "180",
                "rows": 3,
                "cols": 2,
            }
        }),
    )
    assert cfg.display_settings()["screen-v1:monitor-a"] == {
        "selected": True,
        "role": "preview",
        "rotation": "180",
        "rows": 3,
        "cols": 2,
    }


def test_config_preserves_current_monitor_selection():
    cfg = HyperwallConfig(
        server_url="http://localhost:8096",
        username="u",
        password="p",
        last_display_settings=json.dumps({
            "screen-v1:monitor-b": {
                "selected": True,
                "current": True,
                "role": "wall",
                "rotation": "auto",
                "rows": 2,
                "cols": 2,
            }
        }),
    )

    assert cfg.display_settings()["screen-v1:monitor-b"]["current"] is True


def test_config_invalid_stable_display_settings_use_defaults():
    cfg = HyperwallConfig(
        server_url="http://localhost:8096",
        username="u",
        password="p",
        last_display_settings=json.dumps({
            "screen-v1:monitor-a": {
                "selected": "yes",
                "role": "not-a-role",
                "rotation": "sideways",
                "rows": 99,
                "cols": 0,
            }
        }),
    )
    assert cfg.display_settings()["screen-v1:monitor-a"] == {
        "selected": False,
        "role": "wall",
        "rotation": "auto",
        "rows": 6,
        "cols": 1,
    }


def test_wizard_restores_each_monitor_grid_between_sessions():
    if not globals().get("_HAS_PYQT", False):
        raise AssertionError("SKIP")

    from PyQt6.QtCore import QRect
    from hyperwall.wizard import SetupWizard

    class _FakeScreen:
        def __init__(self, name, geometry):
            self._name = name
            self._geometry = geometry

        def name(self):
            return self._name

        def geometry(self):
            return self._geometry

    screens = [
        _FakeScreen("External", QRect(0, 0, 2560, 1440)),
        _FakeScreen("Portrait", QRect(2560, 0, 1440, 2560)),
    ]
    first = SetupWizard(screens, ["Movies"])
    labels = list(first._screen_map)
    for label, value in zip(labels, ((2, 2), (4, 1))):
        first._grid_boxes[label].setCurrentIndex(
            (value[0] - 1) * 6 + value[1] - 1
        )
    saved = first.get_settings()
    first.close()
    first.deleteLater()
    _app.processEvents()

    second = SetupWizard(
        screens,
        ["Movies"],
        last_display_layouts=saved["display_layouts"],
        last_display_settings=saved["display_settings"],
    )
    try:
        restored = [
            normalize_grid_value(second._grid_boxes[label].currentData())
            for label in labels
        ]
        assert restored == [(2, 2), (4, 1)]
    finally:
        second.close()
        second.deleteLater()
        _app.processEvents()



# ── WallController construction (Windows/Qt only) ──

def _build_bare_wall(
    screens, client, display_roles, display_layouts=None,
    preview_rows=3, preview_cols=4,
):
    """Build display widgets without async loading or fullscreen side effects."""
    from hyperwall.wall import WallController

    wall = WallController.__new__(WallController)
    wall.client = client
    wall.screens = screens
    wall.libraries = ["Movies"]
    wall.grid_rows = 2
    wall.grid_cols = 2
    wall.preview_rows = preview_rows
    wall.preview_cols = preview_cols
    wall.display_roles = display_roles
    wall.display_layouts = display_layouts or {}
    wall.cells = []
    wall.windows = []
    wall._window_meta = {}
    wall._solo_cell = None
    wall._solo_window = None
    wall._sync = None
    wall._sync_enabled = False
    wall._shortcuts = []
    wall.controls_visible = False
    wall._build_displays()
    return wall

def test_wall_controller_builds_wall_and_preview_windows():
    if not _PYQT:
        raise AssertionError("SKIP")

    from PyQt6.QtCore import QRect
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

    wall = _build_bare_wall(
        screens,
        _FakeClient(),
        display_roles,
        display_layouts,
        preview_rows=3,
        preview_cols=4,
    )

    # One wall window (3x2 = 6 cells) + one preview window (2x3 = 6 cells)
    assert len(wall.windows) == 2
    assert len(wall.cells) == 12
    meta = wall._window_meta
    roles = {m["role"] for m in meta.values()}
    assert roles == {DisplayRole.WALL, DisplayRole.PREVIEW}
    by_name = {
        meta["screen"].name(): meta for meta in meta.values()
    }
    assert (by_name["External"]["rows"], by_name["External"]["cols"]) == (3, 2)
    assert by_name["External"]["rotation"] == "90"
    assert (by_name["Laptop"]["rows"], by_name["Laptop"]["cols"]) == (2, 3)

    for win in wall.windows:
        win.close()


def test_solo_mode_round_trip():
    if not _PYQT:
        raise AssertionError("SKIP")

    from PyQt6.QtCore import QRect

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

    wall = _build_bare_wall(
        [_FakeScreen("Preview", 0, 0, 1920, 1080)],
        _FakeClient(),
        {"Preview": DisplayRole.PREVIEW},
    )

    cell = wall.cells[0]
    assert wall._solo_cell is None
    from unittest.mock import patch
    with patch("PyQt6.QtWidgets.QWidget.show"):
        wall._enter_solo(cell)
    assert wall._solo_cell is cell
    assert wall._window_meta[id(wall.windows[0])]["solo"] is True
    wall._exit_solo()
    assert wall._solo_cell is None
    assert wall._window_meta[id(wall.windows[0])]["solo"] is False

    for win in wall.windows:
        win.close()


# ── wizard role-switch grid selection ──

def test_role_switch_inherits_session_grid_not_stale_default():
    """Switching a monitor to a role it never had must use the role's most
    recent in-session selection, never a stale launch-time default (the
    2026-08-08 'grid dropdown didn't stay static' regression)."""
    remembered = {DisplayRole.WALL: (4, 1), DisplayRole.PREVIEW: (3, 4)}
    defaults = {DisplayRole.WALL: (2, 2), DisplayRole.PREVIEW: (3, 4)}
    assert grid_for_role_switch(
        remembered, defaults, DisplayRole.WALL, DisplayRole.PREVIEW,
        None, None,
    ) == (3, 4)
    assert grid_for_role_switch(
        remembered, defaults, DisplayRole.PREVIEW, DisplayRole.WALL,
        None, None,
    ) == (4, 1)


def test_role_switch_keeps_monitors_own_saved_layout():
    """A monitor that already had the new role keeps its own saved layout."""
    remembered = {DisplayRole.WALL: (4, 1), DisplayRole.PREVIEW: (3, 4)}
    defaults = {DisplayRole.WALL: (2, 2), DisplayRole.PREVIEW: (3, 4)}
    assert grid_for_role_switch(
        remembered, defaults, DisplayRole.WALL, DisplayRole.WALL, 2, 2,
    ) == (2, 2)


def test_role_switch_validates_and_falls_back():
    remembered = {DisplayRole.WALL: (4, 1), DisplayRole.PREVIEW: (3, 4)}
    defaults = {DisplayRole.WALL: (2, 2), DisplayRole.PREVIEW: (3, 4)}
    # Invalid saved values fall through to the session selection.
    assert grid_for_role_switch(
        remembered, defaults, DisplayRole.WALL, DisplayRole.WALL, "x", None,
    ) == (4, 1)
    # Empty state falls back to the canonical default.
    assert grid_for_role_switch(
        {}, {}, None, DisplayRole.PREVIEW, None, None,
    ) == (2, 2)


# ── saved-grid resolution: identity → name-keyed → role default ──

def test_resolve_saved_grid_identity_wins():
    identity = {"rows": 4, "cols": 1}
    name = {"rows": 2, "cols": 2}
    assert resolve_saved_grid(identity, name, (2, 2)) == (4, 1)


def test_resolve_saved_grid_identity_miss_falls_back_to_name_keyed():
    # The 2026-08-09 "dropdowns weren't sticky" case: stable-identity lookup
    # missed (pure-fallback identity embeds geometry, which drifted), so the
    # always-written name-keyed layout must win over the role default.
    assert resolve_saved_grid(None, {"rows": 4, "cols": 1}, (2, 2)) == (4, 1)


def test_resolve_saved_grid_all_miss_returns_role_default():
    assert resolve_saved_grid(None, None, (3, 4)) == (3, 4)


def test_resolve_saved_grid_rejects_invalid_values():
    assert resolve_saved_grid(
        {"rows": 0, "cols": 7}, {"rows": "x", "cols": None}, (2, 2),
    ) == (2, 2)
    assert resolve_saved_grid(
        {"rows": True, "cols": 3}, None, (2, 2),
    ) == (2, 2)
    # Name-keyed layout wins when the identity layout is present but invalid.
    assert resolve_saved_grid(
        {"rows": 9, "cols": 1}, {"rows": 4, "cols": 2}, (2, 2),
    ) == (4, 2)


def test_resolve_saved_grid_invalid_role_default_falls_to_2x2():
    assert resolve_saved_grid(None, None, (0, 9)) == (2, 2)


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
