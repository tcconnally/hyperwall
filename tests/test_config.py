"""
Unit tests for hyperwall.config (Epic 3) — config save/load round-trip.

No PyQt / mpv / Emby. Uses a temp dir. Run: python tests/test_config.py
"""

from __future__ import annotations

import os
import json
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from hyperwall.config import HyperwallConfig, ConfigMissingError  # noqa: E402


def test_missing_config_creates_template_and_raises():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.ini")
        raised = False
        try:
            HyperwallConfig.load(path)
        except ConfigMissingError:
            raised = True
        assert raised, "expected ConfigMissingError on first load"
        assert os.path.exists(path), "template should have been written"
        # Template must contain both sections.
        with open(path) as f:
            content = f.read()
        assert "[Login]" in content
        assert "[Settings]" in content


def test_save_then_load_round_trips():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.ini")
        cfg = HyperwallConfig(
            server_url="http://emby:8096",
            username="alice",
            password="s3cret",
            verify_ssl=False,
            last_screens="DP-1,DP-2",
            last_libraries="Movies,Music Videos",
            last_grid_rows=3,
            last_grid_cols=4,
            cleanup_on_startup=True,
        )
        cfg.save(path)
        loaded = HyperwallConfig.load(path)
        assert loaded.server_url == "http://emby:8096"
        assert loaded.username == "alice"
        assert loaded.password == "s3cret"
        assert loaded.verify_ssl is False
        assert loaded.last_screens == "DP-1,DP-2"
        assert loaded.last_libraries == "Movies,Music Videos"
        assert loaded.last_grid_rows == 3
        assert loaded.last_grid_cols == 4
        assert loaded.cleanup_on_startup is True


def test_typed_fields_are_correct_types():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.ini")
        HyperwallConfig(
            server_url="http://h", username="u", password="p",
            last_grid_rows=2, last_grid_cols=2,
        ).save(path)
        loaded = HyperwallConfig.load(path)
        # Ints must load as ints, bools as bools — not strings.
        assert isinstance(loaded.last_grid_rows, int)
        assert isinstance(loaded.last_grid_cols, int)
        assert isinstance(loaded.verify_ssl, bool)
        assert isinstance(loaded.cleanup_on_startup, bool)


def test_config_is_frozen():
    cfg = HyperwallConfig(server_url="http://h", username="u", password="p")
    frozen = False
    try:
        cfg.server_url = "mutated"  # type: ignore[misc]
    except Exception:
        frozen = True
    assert frozen, "HyperwallConfig should be an immutable (frozen) dataclass"


def test_defaults_applied_for_absent_settings():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.ini")
        # Write a minimal config with only [Login].
        with open(path, "w") as f:
            f.write("[Login]\nserver_url = http://h\nusername = u\npassword = p\n")
        loaded = HyperwallConfig.load(path)
        assert loaded.last_grid_rows == 2   # fallback default
        assert loaded.last_grid_cols == 2
        assert loaded.cleanup_on_startup is False
        assert loaded.scenes == ()          # no [Scenes] section → empty


def test_display_layouts_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.ini")
        layouts = {
            "External": {"rotation": "90", "rows": 3, "cols": 2},
            "Laptop": {"rotation": "auto", "rows": 3, "cols": 4},
        }
        cfg = HyperwallConfig(
            server_url="http://h", username="u", password="p",
            last_display_layouts=json.dumps(layouts),
        )
        cfg.save(path)
        loaded = HyperwallConfig.load(path)
        assert loaded.display_layouts() == layouts


def test_display_layouts_reject_malformed_entries():
    cfg = HyperwallConfig(
        server_url="http://h", username="u", password="p",
        last_display_layouts=json.dumps({
            "Good": {"rotation": "270", "rows": 6, "cols": 1},
            "BadShape": "not-a-layout",
            "BadRotation": {"rotation": "diagonal", "rows": 2, "cols": 2},
            "BadGrid": {"rotation": "0", "rows": 99, "cols": 0},
        }),
    )
    assert cfg.display_layouts() == {
        "Good": {"rotation": "270", "rows": 6, "cols": 1},
        "BadRotation": {"rotation": "auto", "rows": 2, "cols": 2},
        "BadGrid": {"rotation": "0", "rows": 6, "cols": 1},
    }


def test_scenes_round_trip():
    from hyperwall.scenes import scene_to_str, normalize_scene, scenes_from_mapping
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.ini")
        cinema = normalize_scene("Cinema", {
            "grid_rows": 3, "grid_cols": 4,
            "libraries": ["Movies"], "filter": "favorites",
        })
        HyperwallConfig(
            server_url="http://h", username="u", password="p",
            scenes=(("Cinema", scene_to_str(cinema)),),
        ).save(path)
        loaded = HyperwallConfig.load(path)
        assert len(loaded.scenes) == 1
        name, blob = loaded.scenes[0]
        assert name == "Cinema"
        back = scenes_from_mapping(dict(loaded.scenes))[0]
        assert back["grid_rows"] == 3
        assert back["libraries"] == ["Movies"]
        assert back["filter"] == "favorites"


def run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    sys.exit(run_all())
