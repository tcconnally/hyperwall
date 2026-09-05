"""Pure tests for the soak-only initial corpus filter."""
from __future__ import annotations

from hyperwall.soak_filter import apply_initial_filter


def test_initial_favorites_filter_selects_only_favorites():
    items = [
        {"Id": "a", "UserData": {"IsFavorite": True}},
        {"Id": "b", "UserData": {"IsFavorite": False}},
        {"Id": "c", "UserData": {}},
    ]

    filtered, mode = apply_initial_filter(items, "favorites")

    assert mode == "favorites"
    assert [item["Id"] for item in filtered] == ["a"]
    assert [item["Id"] for item in items] == ["a", "b", "c"]


def test_initial_filter_is_all_by_default_and_for_unknown_modes():
    items = [{"Id": "a"}, {"Id": "b"}]

    for mode in (None, "", "all", "unexpected"):
        filtered, actual_mode = apply_initial_filter(items, mode)
        assert actual_mode == "all"
        assert filtered == items
        assert filtered is not items


def test_exact_item_selection_overrides_pool_filter():
    items = [
        {"Id": "good", "UserData": {"IsFavorite": False}},
        {"Id": "other", "UserData": {"IsFavorite": True}},
    ]

    filtered, mode = apply_initial_filter(
        items, "favorites", item_id="good"
    )

    assert mode == "item"
    assert filtered == [items[0]]


def test_exact_item_selection_fails_closed_when_missing_or_ambiguous():
    items = [
        {"Id": "good"},
        {"Id": "duplicate"},
        {"Id": "duplicate"},
    ]

    missing, missing_mode = apply_initial_filter(
        items, "favorites", item_id="absent"
    )
    ambiguous, ambiguous_mode = apply_initial_filter(
        items, "favorites", item_id="duplicate"
    )

    assert missing == []
    assert missing_mode == "item-not-found"
    assert ambiguous == []
    assert ambiguous_mode == "item-ambiguous"


def run_all() -> int:
    tests = [
        test_initial_favorites_filter_selects_only_favorites,
        test_initial_filter_is_all_by_default_and_for_unknown_modes,
        test_exact_item_selection_overrides_pool_filter,
        test_exact_item_selection_fails_closed_when_missing_or_ambiguous,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures} passed, {failures} failed")
    return failures


if __name__ == "__main__":
    raise SystemExit(run_all())
