"""
Hyperwall — typed configuration management.

Config is loaded once from config.ini, validated, and frozen into a dataclass.
No raw ConfigParser access anywhere else in the codebase.
"""

from __future__ import annotations

import configparser
import json
import os
from dataclasses import dataclass, field

from .constants import CONFIG_FILE, normalize_display_layout


def effective_server_url(configured: str, override: str | None = None) -> str:
    """Return a per-launch endpoint override without changing config.ini.

    This supports controlled LAN-vs-public delivery tests while leaving the
    user's normal configured endpoint and credentials untouched.
    """
    candidate = (override or "").strip()
    return candidate or configured


@dataclass(frozen=True)
class HyperwallConfig:
    """Immutable configuration loaded from config.ini."""

    # ── Login ──
    server_url: str
    username: str
    password: str
    verify_ssl: bool = True
    backend: str = "emby"  # media backend: "emby" | "jellyfin"

    # ── Settings ──
    last_screens: str = ""
    last_libraries: str = ""
    last_grid_rows: int = 2
    last_grid_cols: int = 2
    last_preview_rows: int = 3
    last_preview_cols: int = 4
    last_display_roles: str = ""
    last_display_layouts: str = ""
    cleanup_on_startup: bool = False

    # ── Network sync ──
    sync_enabled: bool = False
    sync_server: bool = False
    sync_host: str = "0.0.0.0"
    sync_port: int = 9876
    sync_display_name: str = ""

    # ── Scenes ──
    # Named wall presets persisted in a [Scenes] section as name=JSON. Stored
    # as a tuple of (name, json_str) pairs to keep the dataclass hashable/frozen.
    scenes: tuple[tuple[str, str], ...] = ()

    @classmethod
    def load(cls, path: str | None = None) -> HyperwallConfig:
        """Load and validate config from disk. Creates template if missing."""
        path = path or CONFIG_FILE
        if not os.path.exists(path):
            cls._create_template(path)
            msg = (
                f"config.ini created at:\n{os.path.abspath(path)}\n\n"
                "Fill in Emby server URL, username, password, then restart."
            )
            raise ConfigMissingError(msg)

        cfg = configparser.ConfigParser()
        cfg.optionxform = str  # preserve case of scene names in [Scenes]
        cfg.read(path)

        scenes = ()
        if cfg.has_section("Scenes"):
            scenes = tuple(
                (name, cfg.get("Scenes", name)) for name in cfg.options("Scenes")
            )

        return cls(
            server_url=cfg.get("Login", "server_url", fallback=""),
            username=cfg.get("Login", "username", fallback=""),
            password=cfg.get("Login", "password", fallback=""),
            verify_ssl=cfg.getboolean("Login", "verify_ssl", fallback=True),
            backend=cfg.get("Login", "backend", fallback="emby"),
            last_screens=cfg.get("Settings", "last_screens", fallback=""),
            last_libraries=cfg.get("Settings", "last_libraries", fallback=""),
            last_grid_rows=cfg.getint("Settings", "last_grid_rows", fallback=2),
            last_grid_cols=cfg.getint("Settings", "last_grid_cols", fallback=2),
            last_preview_rows=cfg.getint(
                "Settings", "last_preview_rows", fallback=3
            ),
            last_preview_cols=cfg.getint(
                "Settings", "last_preview_cols", fallback=4
            ),
            last_display_roles=cfg.get(
                "Settings", "last_display_roles", fallback=""
            ),
            last_display_layouts=cfg.get(
                "Settings", "last_display_layouts", fallback=""
            ),
            cleanup_on_startup=cfg.getboolean(
                "Settings", "cleanup_on_startup", fallback=False
            ),
            sync_enabled=cfg.getboolean(
                "Settings", "sync_enabled", fallback=False
            ),
            sync_server=cfg.getboolean(
                "Settings", "sync_server", fallback=False
            ),
            sync_host=cfg.get("Settings", "sync_host", fallback="0.0.0.0"),
            sync_port=cfg.getint("Settings", "sync_port", fallback=9876),
            sync_display_name=cfg.get(
                "Settings", "sync_display_name", fallback=""
            ),
            scenes=scenes,
        )

    @classmethod
    def _create_template(cls, path: str) -> None:
        """Write a template config.ini."""
        cfg = configparser.ConfigParser()
        cfg["Login"] = {
            "server_url": "http://localhost:8096",
            "username": "",
            "password": "",
            "verify_ssl": "true",
            "backend": "emby",
        }
        cfg["Settings"] = {
            "last_screens": "",
            "last_libraries": "",
            "last_grid_rows": "2",
            "last_grid_cols": "2",
            "last_preview_rows": "3",
            "last_preview_cols": "4",
            "last_display_roles": "",
            "last_display_layouts": "",
            "cleanup_on_startup": "false",
            "sync_enabled": "false",
            "sync_server": "false",
            "sync_host": "0.0.0.0",
            "sync_port": "9876",
            "sync_display_name": "",
        }
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            cfg.write(f)

    def save(self, path: str | None = None) -> None:
        """Write current config back to disk."""
        path = path or CONFIG_FILE
        cfg = configparser.ConfigParser()
        cfg.optionxform = str  # preserve case of scene names in [Scenes]
        cfg["Login"] = {
            "server_url": self.server_url,
            "username": self.username,
            "password": self.password,
            "verify_ssl": str(self.verify_ssl),
            "backend": self.backend,
        }
        cfg["Settings"] = {
            "last_screens": self.last_screens,
            "last_libraries": self.last_libraries,
            "last_grid_rows": str(self.last_grid_rows),
            "last_grid_cols": str(self.last_grid_cols),
            "last_preview_rows": str(self.last_preview_rows),
            "last_preview_cols": str(self.last_preview_cols),
            "last_display_roles": self.last_display_roles,
            "last_display_layouts": self.last_display_layouts,
            "cleanup_on_startup": str(self.cleanup_on_startup),
            "sync_enabled": str(self.sync_enabled),
            "sync_server": str(self.sync_server),
            "sync_host": self.sync_host,
            "sync_port": str(self.sync_port),
            "sync_display_name": self.sync_display_name,
        }
        if self.scenes:
            cfg["Scenes"] = {name: val for name, val in self.scenes}
        with open(path, "w") as f:
            cfg.write(f)

    def display_roles(self) -> dict[str, str]:
        """Parse the JSON last_display_roles map; return {} if malformed."""
        if not self.last_display_roles:
            return {}
        try:
            return json.loads(self.last_display_roles)
        except json.JSONDecodeError:
            return {}

    def display_layouts(self) -> dict[str, dict[str, object]]:
        """Parse and normalize the JSON per-display layout map."""
        if not self.last_display_layouts:
            return {}
        try:
            raw = json.loads(self.last_display_layouts)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(name): normalize_display_layout(layout)
            for name, layout in raw.items()
            if isinstance(layout, dict)
        }


class ConfigMissingError(Exception):
    """Raised when config.ini does not exist and a template was created."""
