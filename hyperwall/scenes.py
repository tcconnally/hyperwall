"""
Hyperwall — pure scene-preset serialization (no PyQt / mpv / Emby).

A "scene" is a named, recallable wall configuration:
    {name, grid_rows, grid_cols, screens[], libraries[], filter}

These functions convert scenes to/from compact JSON strings so config.py can
persist them in a [Scenes] section (one key=value per named scene) and the web
remote can list/apply them. Pure and side-effect-free → unit-testable.
"""

from __future__ import annotations

import json
from typing import Any

VALID_FILTERS = ("all", "favorites")


def _str_list(value: Any) -> list[str]:
    """Coerce to a list of strings. Non-list/tuple values (incl. a bare
    string) yield [] rather than exploding into characters."""
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def normalize_scene(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a partial/loose scene dict into a complete, typed one.

    Missing fields get sensible defaults; types are enforced so a round-trip
    through JSON never yields strings where ints/lists are expected.
    """
    filt = str(raw.get("filter", "all"))
    if filt not in VALID_FILTERS:
        filt = "all"
    return {
        "name": str(name),
        "grid_rows": int(raw.get("grid_rows", 2) or 2),
        "grid_cols": int(raw.get("grid_cols", 2) or 2),
        "screens": _str_list(raw.get("screens")),
        "libraries": _str_list(raw.get("libraries")),
        "filter": filt,
    }


def scene_to_str(scene: dict[str, Any]) -> str:
    """Serialize a scene to a compact JSON string (sans the redundant name)."""
    payload = {
        "grid_rows": int(scene.get("grid_rows", 2)),
        "grid_cols": int(scene.get("grid_cols", 2)),
        "screens": list(scene.get("screens") or []),
        "libraries": list(scene.get("libraries") or []),
        "filter": scene.get("filter", "all"),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def scene_from_str(name: str, s: str) -> dict[str, Any]:
    """Parse a JSON scene string back into a normalized scene dict.

    Malformed JSON yields a default scene (never raises) so one bad config
    entry can't crash startup.
    """
    try:
        raw = json.loads(s)
        if not isinstance(raw, dict):
            raw = {}
    except (ValueError, TypeError):
        raw = {}
    return normalize_scene(name, raw)


def scenes_to_mapping(scenes: list[dict[str, Any]]) -> dict[str, str]:
    """name -> JSON string, for writing a [Scenes] config section."""
    return {str(sc["name"]): scene_to_str(sc) for sc in scenes}


def scenes_from_mapping(mapping: dict[str, str]) -> list[dict[str, Any]]:
    """Inverse of scenes_to_mapping: a [Scenes] section -> list of scenes."""
    return [scene_from_str(name, val) for name, val in mapping.items()]
