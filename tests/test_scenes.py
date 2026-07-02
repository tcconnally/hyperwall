"""
Unit tests for hyperwall.scenes (Epic 4) — pure scene-preset serialization.

No PyQt / mpv / Emby. Run: python tests/test_scenes.py
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from hyperwall.scenes import (  # noqa: E402
    normalize_scene,
    scene_from_str,
    scene_to_str,
    scenes_from_mapping,
    scenes_to_mapping,
)


def test_round_trip_preserves_fields():
    scene = normalize_scene("Cinema", {
        "grid_rows": 3, "grid_cols": 4,
        "screens": ["DP-1", "DP-2"],
        "libraries": ["Movies"],
        "filter": "favorites",
    })
    s = scene_to_str(scene)
    back = scene_from_str("Cinema", s)
    assert back["name"] == "Cinema"
    assert back["grid_rows"] == 3
    assert back["grid_cols"] == 4
    assert back["screens"] == ["DP-1", "DP-2"]
    assert back["libraries"] == ["Movies"]
    assert back["filter"] == "favorites"


def test_normalize_applies_defaults():
    sc = normalize_scene("Bare", {})
    assert sc["grid_rows"] == 2
    assert sc["grid_cols"] == 2
    assert sc["screens"] == []
    assert sc["libraries"] == []
    assert sc["filter"] == "all"


def test_invalid_filter_falls_back_to_all():
    sc = normalize_scene("X", {"filter": "bogus"})
    assert sc["filter"] == "all"


def test_types_are_coerced():
    # Strings from JSON/config must become ints; a non-list 'screens' is
    # rejected to [] rather than exploding into characters (defensive).
    sc = normalize_scene("X", {"grid_rows": "5", "screens": "not-a-list"})
    assert sc["grid_rows"] == 5
    assert sc["screens"] == []
    assert isinstance(sc["screens"], list)


def test_malformed_json_yields_default_scene():
    sc = scene_from_str("Broken", "{not valid json")
    assert sc["name"] == "Broken"
    assert sc["grid_rows"] == 2
    assert sc["filter"] == "all"


def test_non_dict_json_yields_default():
    sc = scene_from_str("Arr", "[1,2,3]")
    assert sc["grid_rows"] == 2


def test_mapping_round_trip():
    scenes = [
        normalize_scene("A", {"grid_rows": 1, "grid_cols": 1, "libraries": ["L1"]}),
        normalize_scene("B", {"grid_rows": 2, "grid_cols": 3, "filter": "favorites"}),
    ]
    mapping = scenes_to_mapping(scenes)
    assert set(mapping.keys()) == {"A", "B"}
    assert all(isinstance(v, str) for v in mapping.values())
    back = scenes_from_mapping(mapping)
    by_name = {s["name"]: s for s in back}
    assert by_name["A"]["libraries"] == ["L1"]
    assert by_name["B"]["grid_cols"] == 3
    assert by_name["B"]["filter"] == "favorites"


def test_serialized_is_compact_json():
    s = scene_to_str(normalize_scene("X", {"grid_rows": 2}))
    assert " " not in s          # compact separators
    assert s.startswith("{") and s.endswith("}")


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
