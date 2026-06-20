"""
Hyperwall v9 — version and package identity.

This module is intentionally dependency-free: it is imported before any
heavy libs to print the runtime banner.
"""

from __future__ import annotations

__version__ = "9.0.0"
APP_NAME = "Hyperwall"
PACKAGE_LABEL = "d3d11-native-embed"

import os
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _git_value(args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=_repo_root(),
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def git_branch() -> str:
    return os.environ.get("HYPERWALL_GIT_BRANCH") or _git_value([
        "branch", "--show-current"
    ]) or "unknown"


def git_commit() -> str:
    return os.environ.get("HYPERWALL_GIT_COMMIT") or _git_value([
        "rev-parse", "--short=12", "HEAD"
    ]) or "unknown"


def entrypoint() -> str:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).name
    return Path(sys.argv[0] or "python").name


def runtime_banner() -> str:
    return (
        f"{APP_NAME} v{__version__} ({PACKAGE_LABEL}) "
        f"entry={entrypoint()} "
        f"frozen={'yes' if getattr(sys, 'frozen', False) else 'no'} "
        f"git={git_branch()}@{git_commit()}"
    )
