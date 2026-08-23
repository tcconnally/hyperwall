"""Pure state helpers for Wizard persistence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


def normalize_grid_value(value: object) -> tuple[int, int] | None:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        return None
    rows, cols = value
    if (
        isinstance(rows, bool)
        or isinstance(cols, bool)
        or not isinstance(rows, int)
        or not isinstance(cols, int)
        or not 1 <= rows <= 6
        or not 1 <= cols <= 6
    ):
        return None
    return rows, cols


def grid_index_for_value(values: Iterable[object], value: object) -> int:
    target = normalize_grid_value(value)
    if target is None:
        return -1
    for index, candidate in enumerate(values):
        if normalize_grid_value(candidate) == target:
            return index
    return -1


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


def grid_for_role_switch(
    remembered: Mapping[str, tuple[int, int]],
    defaults: Mapping[str, tuple[int, int]],
    saved_role: object,
    new_role: object,
    saved_rows: object,
    saved_cols: object,
) -> tuple[int, int]:
    """Return the grid a monitor's dropdown should show when its role changes.

    A monitor that already had the new role keeps its own saved layout;
    otherwise it inherits the role's most recent selection this session.
    Launch-time defaults are only a fallback — a stale default must never
    clobber a grid the user picked minutes ago (2026-08-08: role toggles
    silently reverted in-session grid choices to last session's values).
    """
    if saved_role == new_role:
        if (
            isinstance(saved_rows, int)
            and not isinstance(saved_rows, bool)
            and isinstance(saved_cols, int)
            and not isinstance(saved_cols, bool)
            and 1 <= saved_rows <= 6
            and 1 <= saved_cols <= 6
        ):
            return saved_rows, saved_cols
    return _valid_role_grid(remembered, defaults, new_role)


def resolve_saved_grid(
    identity_layout: Mapping[str, object] | None,
    name_layout: Mapping[str, object] | None,
    role_default: tuple[int, int],
) -> tuple[int, int]:
    """Best saved grid for a display: stable-identity, then name-keyed, then role default.

    Per-display settings are keyed by stable display identity, but the
    pure-fallback identity (no serial/connector/EDID — common on docked
    USB-C displays) includes screen geometry, so an identity miss between
    launches is possible. The name-keyed layout map is written on every
    initialize and its key (the screen name) is far more stable, so it
    backs the identity lookup instead of silently dropping to the role
    default (2026-08-09: wizard grid dropdowns reverted to role defaults
    after an identity miss).
    """
    for layout in (identity_layout, name_layout):
        if not isinstance(layout, Mapping):
            continue
        rows = layout.get("rows")
        cols = layout.get("cols")
        if (
            isinstance(rows, int)
            and not isinstance(rows, bool)
            and isinstance(cols, int)
            and not isinstance(cols, bool)
            and 1 <= rows <= 6
            and 1 <= cols <= 6
        ):
            return rows, cols
    rows, cols = role_default
    if (
        isinstance(rows, int)
        and not isinstance(rows, bool)
        and isinstance(cols, int)
        and not isinstance(cols, bool)
        and 1 <= rows <= 6
        and 1 <= cols <= 6
    ):
        return rows, cols
    return (2, 2)


def _valid_role_grid(
    remembered: Mapping[str, tuple[int, int]],
    defaults: Mapping[str, tuple[int, int]],
    role: object,
) -> tuple[int, int]:
    """First valid (rows, cols) for a role from remembered, then defaults."""
    for source in (remembered, defaults):
        value = source.get(role)  # type: ignore[arg-type]
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], int)
            and not isinstance(value[0], bool)
            and isinstance(value[1], int)
            and not isinstance(value[1], bool)
            and 1 <= value[0] <= 6
            and 1 <= value[1] <= 6
        ):
            return int(value[0]), int(value[1])
    return (2, 2)
