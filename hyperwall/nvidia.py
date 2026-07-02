"""
Hyperwall — NVIDIA Profile Inspector integration.

Manages per-app G-Sync disable via NVIDIA Profile Inspector.
Isolation is enabled when the process runs as a bundled `hyperwall*.exe`
(any version suffix) or when HYPERWALL_ISOLATED=1 is set, so the exe can be
renamed across releases without silently disabling G-Sync isolation.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import shutil
import subprocess
import sys

from .constants import (
    LAUNCH_BASENAME,
    LAUNCHER_EXE,
    NIP_FILE,
    NPI_EXE,
    NV_SENTINEL,
    SCRIPT_DIR,
)

logger = logging.getLogger("HyperWall")

_IS_WINDOWS = platform.system() == "Windows"


def _is_isolated_launch() -> bool:
    """True when we're running as the bundled, G-Sync-isolated executable.

    Decoupled from any specific version-suffixed name: a frozen exe whose
    basename starts with 'hyperwall' and ends in '.exe' (e.g. hyperwall.exe)
    qualifies, as does an explicit HYPERWALL_ISOLATED=1 override. This is what
    lets us rename the exe across releases without silently losing isolation.
    """
    if os.environ.get("HYPERWALL_ISOLATED") == "1":
        return True
    name = LAUNCH_BASENAME
    return name.startswith("hyperwall") and name.endswith(".exe")

_NPI_SEARCH_DIRS = [
    os.environ.get("NPI_PATH", ""),
    os.environ.get("PROGRAMFILES", ""),
    os.environ.get("PROGRAMFILES(X86)", ""),
    os.path.expanduser(r"~\Downloads"),
    os.path.expanduser("~"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
]


def _find_npi() -> str | None:
    """Locate nvidiaProfileInspector.exe, returning the path or None."""
    if os.path.exists(NPI_EXE):
        return NPI_EXE
    for base in _NPI_SEARCH_DIRS:
        if not base:
            continue
        for sub in ("", "tools", "bin"):
            cand = os.path.join(base, sub, "nvidiaProfileInspector.exe")
            if os.path.exists(cand):
                return cand
    return (
        shutil.which("nvidiaProfileInspector.exe")
        or shutil.which("nvidiaProfileInspector")
        or None
    )


def nv_driver_version() -> str | None:
    """Return the installed NVIDIA driver version, or None."""
    if not _IS_WINDOWS:
        return None
    try:
        extra: dict[str, int] = {}
        if _IS_WINDOWS:
            extra["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            text=True, timeout=5, **extra,
        )
        return out.strip().splitlines()[0]
    except Exception:
        return None


def ensure_nvidia_profile() -> bool:
    """Apply the HyperWall NVIDIA profile if needed.

    Returns True if the profile was applied or is already current.
    Returns False if NPI is unavailable (non-fatal — wall runs without isolation).
    """
    if not _IS_WINDOWS:
        return True

    if not _is_isolated_launch():
        logger.warning(
            "G-Sync isolation disabled — running as '%s', not a bundled "
            "hyperwall*.exe. Build via build.bat (or set HYPERWALL_ISOLATED=1) "
            "for full isolation.", LAUNCH_BASENAME,
        )
        return False

    npi_exe = _find_npi()
    if not npi_exe:
        logger.warning(
            "nvidiaProfileInspector.exe not found — install it to enable "
            "G-Sync isolation."
        )
        return False

    if not os.path.exists(NIP_FILE):
        logger.warning(
            "Missing NVIDIA profile %s — isolation skipped.", NIP_FILE
        )
        return False

    drv = nv_driver_version()
    if not drv:
        logger.warning(
            "Could not read NVIDIA driver version — skipping profile check."
        )
        return False

    # Check sentinel
    if os.path.exists(NV_SENTINEL):
        try:
            with open(NV_SENTINEL, encoding="utf-8") as f:
                if f.read().strip() == drv:
                    logger.info("NVIDIA profile current (driver %s).", drv)
                    return True
        except Exception:
            pass

    # Apply profile
    logger.info(
        "Applying NVIDIA profile (driver %s) — UAC elevation required.", drv
    )
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", npi_exe,
            f'-silentImport "{NIP_FILE}"', SCRIPT_DIR, 0,  # SW_HIDE
        )
        if rc <= 32:
            logger.warning(
                "ShellExecuteW returned %d — NPI did not launch.", rc
            )
            return False
        with open(NV_SENTINEL, "w", encoding="utf-8") as f:
            f.write(drv)
        logger.info("NVIDIA profile applied; sentinel written.")
        return True
    except Exception as e:
        logger.warning("Failed to apply NVIDIA profile: %s", e)
        return False


def maybe_relaunch_in_isolation() -> None:
    """Re-exec into the bundled .exe for NVIDIA process isolation.

    When running as 'python hyperwall.py', relaunch via the bundled exe
    so the NVIDIA driver matches the per-app G-Sync profile.
    """
    if _is_isolated_launch():
        return
    if not os.path.exists(LAUNCHER_EXE):
        return
    if os.environ.get("HYPERWALL_NO_RELAUNCH") == "1":
        logger.info(
            "Re-launch suppressed (HYPERWALL_NO_RELAUNCH=1) — "
            "script mode, no isolation."
        )
        return
    logger.info("Re-launching via isolated exe: %s", LAUNCHER_EXE)
    try:
        subprocess.Popen(
            [LAUNCHER_EXE] + sys.argv[1:],
            cwd=SCRIPT_DIR,
            close_fds=True,
        )
        sys.exit(0)
    except Exception as e:
        logger.warning(
            "Re-launch failed (%s) — continuing in current process.", e
        )
