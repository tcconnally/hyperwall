"""Pure helpers for selecting an initial soak corpus."""
from __future__ import annotations

from typing import Any


def apply_initial_filter(
    items: list[dict[str, Any]],
    mode: str | None,
) -> tuple[list[dict[str, Any]], str]:
    """Return the initial playback pool and its normalized filter mode.

    The soak-only caller uses ``favorites`` to select a stable corpus before
    any cell starts. Normal launches pass an empty mode and retain all items.
    """
    normalized = str(mode or "").strip().lower()
    if normalized != "favorites":
        return list(items), "all"
    return [
        item
        for item in items
        if isinstance(item.get("UserData"), dict)
        and item["UserData"].get("IsFavorite") is True
    ], "favorites"
