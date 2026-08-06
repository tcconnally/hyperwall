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
    """
    serial = _screen_text(screen, "serialNumber")
    if serial.casefold() in {"0", "unknown", "none", "n/a"}:
        serial = ""
    manufacturer = _screen_text(screen, "manufacturer")
    model = _screen_text(screen, "model")
    name = _screen_text(screen, "name")
    connector = _screen_text(screen, "connectorName") or _screen_text(screen, "edidHash")
    material = (
        "serial|" + "|".join((manufacturer, model, serial))
        if serial
        else (
            "connector|" + "|".join((manufacturer, model, connector))
            if connector
            else "fallback|" + "|".join((manufacturer, model, name))
        )
    )
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
    return {"selected": selected, "role": role, **layout}


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
