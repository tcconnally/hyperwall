"""
Hyperwall v9 — application bootstrap and main().

Orchestrates startup: DLL registration, NVIDIA profile, config loading,
Emby authentication, wizard, wall launch, and web remote.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import requests

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from . import __version__, runtime_banner
from .config import HyperwallConfig, ConfigMissingError
from .constants import (
    CONFIG_FILE,
    LOG_FILE,
    MOUSE_IDLE_MS,
    MPV_OPTS,
    SCRIPT_DIR,
    STATS_ENABLED,
    apply_env_overrides,
)
from .emby import EmbyClient, CleanupWorker
from .nvidia import ensure_nvidia_profile, maybe_relaunch_in_isolation
from .wizard import SetupWizard
from .wall import WallController, MouseIdleHider

logger = logging.getLogger("HyperWall")

_WEB_AVAILABLE = False
try:
    from . import web as _web
    _WEB_AVAILABLE = True
except ImportError:
    pass


# ── mpv DLL registration ─────────────────────────────────────────────────────
# Must happen once, before any mpv import. The cookie must stay alive
# (held at module level) to prevent GC from removing the DLL directory.

_mpv_dll_cookie = None

if os.name == "nt":
    _dll_dirs = [SCRIPT_DIR]
    if getattr(sys, "frozen", False):
        _dll_dirs.insert(0, sys._MEIPASS)

    for _d in _dll_dirs:
        if os.path.isdir(_d):
            try:
                _mpv_dll_cookie = os.add_dll_directory(_d)
            except AttributeError:
                os.environ["PATH"] = (
                    _d + os.pathsep + os.environ.get("PATH", "")
                )

    # Also prepend to PATH for python-mpv's internal loader
    if getattr(sys, "frozen", False):
        os.environ["PATH"] = (
            sys._MEIPASS + os.pathsep + os.environ.get("PATH", "")
        )


# ── logging setup ────────────────────────────────────────────────────────────


class MPVLogFilter(logging.Filter):
    """Suppress known mpv log noise."""

    _NOISE = (
        "UDTA parsing failed retrying raw",
        "Detected creation time before 1970",
        "Unknown cover type",
        "stream 0, timescale not set",
        "client removed during hook handling",
        "Immediate exit requested",
        "Leaking 1 nested connections",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if "mpv[" in record.msg and any(
            pat in record.msg for pat in self._NOISE
        ):
            return False
        return True


def _setup_logging() -> None:
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S"
    )
    mpv_filter = MPVLogFilter()

    if not os.environ.get("HYPERWALL_NO_LOG_SETUP"):
        fh = RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.addFilter(mpv_filter)
        logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    ch.addFilter(mpv_filter)
    logger.addHandler(ch)


# ── exception hook ───────────────────────────────────────────────────────────


def _handle_exception(et: type, ev: BaseException, tb: object) -> None:
    if issubclass(et, KeyboardInterrupt):
        sys.__excepthook__(et, ev, tb)
        return
    logger.critical("UNHANDLED EXCEPTION", exc_info=(et, ev, tb))


# ── helpers ──────────────────────────────────────────────────────────────────


def _ordered_screens(app: QApplication) -> list:
    """Return screens sorted left-to-right like Windows Display Settings."""
    screens = list(app.screens())
    if not screens:
        return screens
    primary = app.primaryScreen()
    others = [s for s in screens if s is not primary]
    others.sort(key=lambda s: (s.geometry().x(), s.geometry().y()))
    return [primary] + others if primary in screens else others


def _show_config_created_dialog(msg: str) -> None:
    """Show a modal dialog about the config template."""
    app = QApplication.instance() or QApplication(sys.argv)
    QMessageBox.information(None, "Config Created", msg)


def _show_error_dialog(title: str, msg: str) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    QMessageBox.critical(None, title, msg)


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    sys.excepthook = _handle_exception

    # 1. NVIDIA isolation: re-exec into bundled exe if needed
    maybe_relaunch_in_isolation()

    # 2. Verify libmpv is importable
    try:
        import mpv  # noqa: F401
    except Exception as e:
        msg = (
            f"python-mpv failed to load: {e}\n\n"
            f"Install:\n  pip install python-mpv\n\n"
            f"And place mpv-2.dll next to this script:\n  {SCRIPT_DIR}\n\n"
            f"Download: https://sourceforge.net/projects/mpv-player-windows/files/libmpv/\n"
            f"  (shinchiro build — extract libmpv-2.dll, place in script dir)"
        )
        try:
            QApplication(sys.argv)
            QMessageBox.critical(None, "HyperWall — libmpv missing", msg)
        except Exception:
            print(msg, file=sys.stderr)
        sys.exit(1)

    # 3. Logging
    _setup_logging()
    logger.info("Runtime: %s", runtime_banner())

    # 4. Process priority (HIGH)
    if os.name == "nt" and not os.environ.get("HYPERWALL_NO_LOG_SETUP"):
        try:
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080
            )
            logger.info("Kernel: Priority set to HIGH.")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 5. NVIDIA profile
    ensure_nvidia_profile()

    # Mouse idle hider
    _mouse_hider = MouseIdleHider(MOUSE_IDLE_MS)  # noqa: F841

    # 6. Config
    try:
        cfg = HyperwallConfig.load()
    except ConfigMissingError as e:
        logger.info(str(e))
        _show_config_created_dialog(str(e))
        sys.exit(0)

    if not cfg.server_url or not cfg.username:
        _show_error_dialog(
            "Config Error",
            "server_url and username must be set in config.ini.",
        )
        sys.exit(1)

    # 7. Emby client
    client = EmbyClient(
        cfg.server_url, cfg.username, cfg.password, verify_ssl=cfg.verify_ssl,
    )
    if not cfg.verify_ssl:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logger.warning(
            "SSL verification disabled — set verify_ssl=true for production."
        )

    if not client.test_connection():
        _show_error_dialog(
            "Connection Error",
            f"Cannot reach Emby server at:\n{cfg.server_url}",
        )
        sys.exit(1)

    if not client.authenticate():
        _show_error_dialog(
            "Auth Error",
            "Authentication failed.\nCheck username and password.",
        )
        sys.exit(1)

    # 8. Optional cleanup
    if cfg.cleanup_on_startup:
        _run_cleanup_dialog(client)

    # 9. Fetch libraries
    libraries = client.fetch_libraries()

    # 10. Wizard
    ordered_screens = _ordered_screens(app)
    wiz = SetupWizard(
        ordered_screens,
        libraries,
        last_screens=cfg.last_screens,
        last_libraries=cfg.last_libraries,
        last_rows=cfg.last_grid_rows,
        last_cols=cfg.last_grid_cols,
    )
    if wiz.exec() != QDialog.DialogCode.Accepted:
        client.close()
        sys.exit(0)

    settings = wiz.get_settings()
    if not settings["screens"] or not settings["libraries"]:
        _show_error_dialog(
            "Setup Error",
            "Select at least one display and one library.",
        )
        client.close()
        sys.exit(1)

    # Save selections back to config
    cfg = HyperwallConfig(
        server_url=cfg.server_url,
        username=cfg.username,
        password=cfg.password,
        verify_ssl=cfg.verify_ssl,
        last_screens=",".join(s.name() for s in settings["screens"]),
        last_libraries=",".join(settings["libraries"]),
        last_grid_rows=settings["grid_rows"],
        last_grid_cols=settings["grid_cols"],
    )
    cfg.save()

    # 11. Perf env
    _eff = apply_env_overrides(MPV_OPTS)
    logger.info(
        "Perf: vo=%s gpu_api=%s hwdec=%s profile=%s video_sync=%s "
        "hdr_hint=%s stats=%s",
        _eff.get("vo"), _eff.get("gpu_api"), _eff.get("hwdec"),
        _eff.get("profile"), _eff.get("video_sync"),
        _eff.get("target_colorspace_hint"),
        "on" if STATS_ENABLED else "off",
    )

    # 12. Launch wall
    logger.info("Initializing HyperWall %s…", __version__)
    wall = WallController(
        screens=settings["screens"],
        libraries=settings["libraries"],
        grid_rows=settings["grid_rows"],
        grid_cols=settings["grid_cols"],
        client=client,
    )

    if _WEB_AVAILABLE:
        _web.start(wall)
    else:
        logger.info("Web remote unavailable (flask not installed).")

    app.aboutToQuit.connect(wall._cleanup)
    sys.exit(app.exec())


def _run_cleanup_dialog(client: EmbyClient) -> None:
    """Show a modal cleanup progress dialog."""
    from PyQt6.QtCore import QThread

    dlg = QDialog()
    dlg.setWindowFlags(Qt.WindowType.FramelessWindowHint)
    dlg.setStyleSheet("background: #111; border: 1px solid #2a2a2a;")
    dlg.setMinimumWidth(340)

    dl = QVBoxLayout(dlg)
    dl.setContentsMargins(28, 22, 28, 22)
    lbl = QLabel("SYSTEM MAINTENANCE\nPurging tagged items…")
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet(
        "color: #3b8edb; font-weight: bold; font-size: 13px;"
        " font-family: 'Segoe UI'; background: transparent;"
    )
    dl.addWidget(lbl)

    t = QThread()
    w = CleanupWorker(client)
    w.moveToThread(t)
    w.progress.connect(lambda name: lbl.setText(f"PURGING:\n{name[:42]}"))
    w.finished.connect(lambda ok, fail: (
        logger.info("Maintenance: %d deleted, %d failed.", ok, fail),
        t.quit(),
        dlg.accept(),
    ))
    t.started.connect(w.run)
    t.start()
    dlg.exec()
    t.wait()
