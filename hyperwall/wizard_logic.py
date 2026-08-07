"""Pure state helpers for Wizard persistence."""

from __future__ import annotations

from collections.abc import Mapping


def update_last_selected_grid(
    remembered: Mapping[str, tuple[int, int]],
    role: object,
    value: object,
) -> dict[str, tuple[int, int]]:
    """Return ``remembered`` with one valid role grid replaced.

    The Wizard has one grid selector per display, but config also stores a
    role-level default for new displays and future launches. The role-level
    value must follow the most recent valid selection, not display-list order.
    """
    updated = dict(remembered)
    if not isinstance(role, str) or role not in updated:
        return updated
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return updated
    rows, cols = value
    if (
        isinstance(rows, bool)
        or isinstance(cols, bool)
        or not isinstance(rows, int)
        or not isinstance(cols, int)
        or not 1 <= rows <= 6
        or not 1 <= cols <= 6
    ):
        return updated
    updated[role] = (rows, cols)
    return updated
