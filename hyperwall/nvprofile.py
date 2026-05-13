import os
import sys
import logging
import subprocess
import platform
import ctypes

logger = logging.getLogger("HyperWall")

def nv_driver_version() -> str | None:
    if platform.system() != "Windows":
        return None
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            text=True, timeout=5, creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        return out.strip().splitlines()[0]
    except Exception:
        return None

def ensure_nvidia_profile(launch_basename: str, nip_file: str, npi_exe: str, nv_sentinel: str, script_dir: str) -> None:
    if platform.system() != "Windows":
        return
    if launch_basename != "hyperwall_v8.exe":
        logger.warning(
            "G-Sync isolation disabled — running as '%s', not hyperwall_v8.exe. "
            "Build via build_v8.bat for full isolation.", launch_basename,
        )
        return
    if not os.path.exists(nip_file):
        logger.warning("Missing NVIDIA profile %s — isolation skipped.", nip_file)
        return
    if not os.path.exists(npi_exe):
        logger.warning("Missing %s — install nvidiaProfileInspector to enable isolation.", npi_exe)
        return

    drv = nv_driver_version()
    if not drv:
        logger.warning("Could not read NVIDIA driver version — skipping profile check.")
        return

    if os.path.exists(nv_sentinel):
        try:
            with open(nv_sentinel, encoding="utf-8") as f:
                if f.read().strip() == drv:
                    logger.info("NVIDIA profile current (driver %s).", drv)
                    return
        except Exception:
            pass

    logger.info("Applying NVIDIA profile (driver %s) — UAC elevation required.", drv)
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", npi_exe,
            f'-silentImport "{nip_file}"', script_dir, 0,  # SW_HIDE
        )
        if rc <= 32:
            logger.warning("ShellExecuteW returned %d — NPI did not launch.", rc)
            return
        with open(nv_sentinel, "w", encoding="utf-8") as f:
            f.write(drv)
        logger.info("NVIDIA profile applied; sentinel written.")
    except Exception as e:
        logger.warning("Failed to apply NVIDIA profile: %s", e)

def maybe_relaunch_in_isolation(launch_basename: str, launcher_exe: str, script_dir: str) -> None:
    if platform.system() != "Windows":
        return
    if launch_basename == "hyperwall_v8.exe":
        return
    if not os.path.exists(launcher_exe):
        return
    if os.environ.get("HYPERWALL_NO_RELAUNCH") == "1":
        logger.info("Re-launch suppressed (HYPERWALL_NO_RELAUNCH=1) — script mode, no isolation.")
        return
    logger.info("Re-launching via isolated exe: %s", launcher_exe)
    try:
        subprocess.Popen([launcher_exe] + sys.argv[1:], cwd=script_dir, close_fds=True)
        sys.exit(0)
    except Exception as e:
        logger.warning("Re-launch failed (%s) — continuing in current process.", e)
