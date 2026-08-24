"""Stable display identity and persisted-setting helpers."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from .constants import DisplayRole, DisplayRotation, normalize_display_layout


def _screen_text(screen: Any, method: str) -> str:
    """Read a QScreen metadata method without requiring Qt in headless tests."""
    value = getattr(screen, method, None)
    if not callable(value):
        return ""
    try:
        return str(value() or "").strip()
    except Exception:  # pragma: no cover - defensive for platform Qt wrappers
        return ""


def display_identity(screen: Any) -> str:
    """Return an opaque, stable identity for a physical display.

    Prefer the OS-provided display serial number. When a platform does not
    expose one, use manufacturer, model, and connector name as a deterministic
    best-effort fallback. No display-list index is part of the identity.

    When serial and connector are both unavailable (e.g. identical monitors
    on a Qt version where those APIs aren't present), include the screen's
    geometry to disambiguate physically distinct displays — two monitors of
    the same model must live at different positions.
    """
    serial = _screen_text(screen, "serialNumber")
    if serial.casefold() in {"0", "unknown", "none", "n/a"}:
        serial = ""
    manufacturer = _screen_text(screen, "manufacturer")
    model = _screen_text(screen, "model")
    name = _screen_text(screen, "name")
    connector = _screen_text(screen, "connectorName") or _screen_text(screen, "edidHash")
    if serial:
        material = "serial|" + "|".join((manufacturer, model, serial))
    elif connector:
        material = "connector|" + "|".join((manufacturer, model, connector))
    else:
        # Pure-fallback: two identical monitors on a Qt version without
        # serial, connector, or EDID metadata. Geometry disambiguates them.
        geometry = getattr(screen, "geometry", None)
        try:
            geo = geometry() if callable(geometry) else None
        except Exception:
            geo = None
        if geo is not None:
            pos = f"{geo.x()},{geo.y()},{geo.width()}x{geo.height()}"
        else:
            pos = name
        material = "fallback|" + "|".join((manufacturer, model, pos))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"screen-v1:{digest}"


def normalize_display_settings(
    raw: object | None,
    *,
    wall_grid: tuple[int, int] = (2, 2),
    preview_grid: tuple[int, int] = (3, 4),
) -> dict[str, object]:
    """Return one monitor's persisted settings with safe role-aware defaults."""
    data = raw if isinstance(raw, dict) else {}
    role_value = data.get("role", DisplayRole.WALL)
    role = role_value if isinstance(role_value, str) else DisplayRole.WALL
    role = role.strip().lower()
    if not DisplayRole.is_valid(role):
        role = DisplayRole.WALL

    defaults = preview_grid if role == DisplayRole.PREVIEW else wall_grid
    selected = data.get("selected", False)
    if not isinstance(selected, bool):
        selected = False
    layout = normalize_display_layout({
        "rotation": data.get("rotation", DisplayRotation.AUTO),
        "rows": data.get("rows", defaults[0]),
        "cols": data.get("cols", defaults[1]),
    })
    result = {"selected": selected, "role": role, **layout}
    current = data.get("current")
    if isinstance(current, bool):
        result["current"] = current
    return result


def restore_display_settings(
    screen: Any,
    persisted: Mapping[str, object] | None,
    *,
    wall_grid: tuple[int, int] = (2, 2),
    preview_grid: tuple[int, int] = (3, 4),
) -> dict[str, object]:
    """Restore a screen by stable identity; missing screens receive defaults."""
    raw = (persisted or {}).get(display_identity(screen))
    return normalize_display_settings(
        raw, wall_grid=wall_grid, preview_grid=preview_grid
    )


__all__ = [
    "display_identity",
    "normalize_display_settings",
    "restore_display_settings",
]
