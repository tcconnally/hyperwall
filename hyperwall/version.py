"""Runtime identity helpers for HyperWall.

Keep this intentionally dependency-light: it is imported during startup before the
wall is created and should work in both source and PyInstaller builds.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "HyperWall"
APP_VERSION = "8.2"
PACKAGE_LABEL = "d3d11-native-embed"


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
    value = result.stdout.strip()
    return value or None


def git_branch() -> str:
    return os.environ.get("HYPERWALL_GIT_BRANCH") or _git_value(["branch", "--show-current"]) or "unknown"


def git_commit() -> str:
    return os.environ.get("HYPERWALL_GIT_COMMIT") or _git_value(["rev-parse", "--short=12", "HEAD"]) or "unknown"


def entrypoint() -> str:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).name
    return Path(sys.argv[0] or "python").name


def runtime_identity() -> dict[str, str]:
    return {
        "app": APP_NAME,
        "version": APP_VERSION,
        "label": PACKAGE_LABEL,
        "entrypoint": entrypoint(),
        "frozen": "yes" if getattr(sys, "frozen", False) else "no",
        "git_branch": git_branch(),
        "git_commit": git_commit(),
    }


def runtime_banner() -> str:
    ident = runtime_identity()
    return (
        f"{ident['app']} v{ident['version']} ({ident['label']}) "
        f"entry={ident['entrypoint']} frozen={ident['frozen']} "
        f"git={ident['git_branch']}@{ident['git_commit']}"
    )
