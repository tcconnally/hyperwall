"""Pure helpers for selecting an initial soak corpus."""
from __future__ import annotations

from typing import Any


def apply_initial_filter(
    items: list[dict[str, Any]],
    mode: str | None,
    *,
    item_id: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Return the initial soak pool and its normalized selection mode.

    ``item_id`` is an opt-in exact-resource selector for controlled native
    decoder experiments.  It takes precedence over the broad pool filter and
    fails closed when the source response does not contain exactly one match.
    Normal launches pass no selector and retain the existing all/favorites
    behavior.
    """
    if item_id not in (None, ""):
        matches = [item for item in items if item.get("Id") == item_id]
        if not matches:
            return [], "item-not-found"
        if len(matches) != 1:
            return [], "item-ambiguous"
        return list(matches), "item"

    normalized = str(mode or "").strip().lower()
    if normalized != "favorites":
        return list(items), "all"
    return [
        item
        for item in items
        if isinstance(item.get("UserData"), dict)
        and item["UserData"].get("IsFavorite") is True
    ], "favorites"
