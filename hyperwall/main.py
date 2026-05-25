import os
import sys
import ctypes
import logging
import configparser
import requests # Added for RequestException
import requests.exceptions # Added for RequestException
from PyQt6.QtCore import Qt, QThread, QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QVBoxLayout, QLabel, QMessageBox

from .perf import (
    logger, setup_logging, MPV_OPTS, STATS_ENABLED, apply_perf_env,
    SCRIPT_DIR, CONFIG_FILE, LOG_FILE, LAUNCHER_EXE, NIP_FILE, NPI_EXE, 
    NV_SENTINEL, LAUNCH_BASENAME
)
from .nvprofile import ensure_nvidia_profile, maybe_relaunch_in_isolation
from .emby import EmbyAPISession, CleanupWorker
from .wizard import SetupWizard
from .controller import WallController, MouseIdleHider
from .version import runtime_banner
from .style import BG_DEEP, BG_SURFACE, BG_RAISED, CYAN, MAGENTA, TEXT, TEXT_DIM, BORDER, GLOBAL_QSS

# Late import for mpv
# On Windows, Python 3.8+ tightened DLL search — os.add_dll_directory()
# is the correct API to make script-dir DLLs visible to ctypes.
# (PATH manipulation alone is not sufficient.)
# NOTE: add_dll_directory() returns a cookie that MUST be kept alive.
# If it's garbage-collected, the directory is removed from the search path.
_mpv_dll_cookie = None
if os.name == "nt":
    _mpv_dll_dir = SCRIPT_DIR
    if os.path.isdir(_mpv_dll_dir):
        try:
            _mpv_dll_cookie = os.add_dll_directory(_mpv_dll_dir)
        except AttributeError:
            # Python < 3.8 fallback
            os.environ["PATH"] = _mpv_dll_dir + os.pathsep + os.environ.get("PATH", "")

mpv = None
_MPV_IMPORT_ERR = None
try:
    import mpv as _mpv
    mpv = _mpv
except Exception as e:
    _MPV_IMPORT_ERR = e

def _handle_exception(et, ev, tb):
    if issubclass(et, KeyboardInterrupt):
        sys.__excepthook__(et, ev, tb)
        return
    logger.critical("UNHANDLED EXCEPTION", exc_info=(et, ev, tb))


def _ordered_screens(app):
    """Return screens sorted to match Windows monitor numbering (left-to-right, top-to-bottom)."""
    screens = list(app.screens())
    if not screens:
        return screens
    primary = app.primaryScreen()
    others = [s for s in screens if s is not primary]
    # Sort by virtual position (x then y) — this usually matches Windows Display Settings order
    others.sort(key=lambda s: (s.geometry().x(), s.geometry().y()))
    ordered = [primary] + others if primary in screens else others
    return ordered

def main():
    sys.excepthook = _handle_exception

    # 1. NVIDIA isolation: re-exec into bundled launcher if present.
    maybe_relaunch_in_isolation(LAUNCH_BASENAME, LAUNCHER_EXE, SCRIPT_DIR)

    # 2. libmpv presence check
    if mpv is None:
        msg = (f"python-mpv failed to load: {_MPV_IMPORT_ERR}\n\n"
               f"Install:\n  pip install python-mpv\n\n"
               f"And place mpv-2.dll next to this script:\n  {SCRIPT_DIR}\n\n"
               f"Download: https://sourceforge.net/projects/mpv-player-windows/files/libmpv/\n"
               f"  (shinchiro build — extract libmpv-2.dll, place in script dir)")
        logger.critical(msg)
        try:
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "HyperWall — libmpv missing", msg)
        except Exception:
            print(msg, file=sys.stderr)
        sys.exit(1)

    # 3. Setup logging
    setup_logging(LOG_FILE)
    logger.info("Runtime: %s", runtime_banner())

    # 4. Set priority — ABOVE_NORMAL for a foreground video wall. HIGH (0x80)
    # risks starving network I/O and preventing CPU C-state transitions during
    # idle periods between I/O.  mpv manages its own critical threads via MMCSS
    # ("Playback" / "Pro Audio") which are always above process priority.
    if not os.environ.get("HYPERWALL_NO_LOG_SETUP"):
        try:
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), 0x00008000  # ABOVE_NORMAL
            )
            logger.info("Kernel: Priority set to ABOVE_NORMAL.")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # ── Cyberpunk dark Fusion palette (native widget colours) ───────────
    from PyQt6.QtGui import QPalette, QColor
    p = app.palette()
    p.setColor(QPalette.ColorRole.Window,          QColor(BG_DEEP))
    p.setColor(QPalette.ColorRole.WindowText,       QColor(TEXT))
    p.setColor(QPalette.ColorRole.Base,             QColor(BG_RAISED))
    p.setColor(QPalette.ColorRole.AlternateBase,    QColor(BG_SURFACE))
    p.setColor(QPalette.ColorRole.ToolTipBase,      QColor(BG_SURFACE))
    p.setColor(QPalette.ColorRole.ToolTipText,      QColor(CYAN))
    p.setColor(QPalette.ColorRole.Text,             QColor(TEXT))
    p.setColor(QPalette.ColorRole.Button,           QColor(BG_RAISED))
    p.setColor(QPalette.ColorRole.ButtonText,       QColor(CYAN))
    p.setColor(QPalette.ColorRole.BrightText,       QColor(CYAN))
    p.setColor(QPalette.ColorRole.Link,             QColor(CYAN))
    p.setColor(QPalette.ColorRole.Highlight,        QColor(CYAN))
    p.setColor(QPalette.ColorRole.HighlightedText,  QColor(BG_DEEP))
    p.setColor(QPalette.ColorRole.PlaceholderText,  QColor(TEXT_DIM))
    app.setPalette(p)
    app.setStyleSheet(GLOBAL_QSS)

    # 5. Verify/apply NVIDIA profile
    ensure_nvidia_profile(LAUNCH_BASENAME, NIP_FILE, NPI_EXE, NV_SENTINEL, SCRIPT_DIR)

    from .perf import MOUSE_IDLE_MS
    _mouse_hider = MouseIdleHider(MOUSE_IDLE_MS)  # noqa: F841

    cfg = configparser.ConfigParser()

    if not os.path.exists(CONFIG_FILE):
        cfg["Login"] = {"server_url": "http://localhost:8096", "username": "", "password": ""}
        cfg["Settings"] = {
            "last_screens": "", "last_libraries": "",
            "last_grid_rows": "2", "last_grid_cols": "2",
            "cleanup_on_startup": "false",
        }
        with open(CONFIG_FILE, "w") as f:
            cfg.write(f)
        msg = (f"config.ini created at:\n{os.path.abspath(CONFIG_FILE)}\n\n"
               "Fill in Emby server URL, username, password, then restart.")
        logger.info(msg)
        QMessageBox.information(
            None, "Config Created", msg
        )
        sys.exit(0)

    cfg.read(CONFIG_FILE)
    s_url  = cfg.get("Login", "server_url", fallback="")
    s_user = cfg.get("Login", "username",   fallback="")
    s_pass = cfg.get("Login", "password",   fallback="")
    if not s_url or not s_user:
        msg = "server_url and username must be set in config.ini."
        logger.critical(f"Config Error: {msg}")
        QMessageBox.critical(None, "Config Error", msg)
        sys.exit(1)

    api = EmbyAPISession(s_url, s_user, s_pass)
    api.verify_ssl = cfg.getboolean("Login", "verify_ssl", fallback=True)
    if not api.verify_ssl:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        logger.warning("SSL verification disabled — set verify_ssl = true in config.ini for production.")
    if not api.test_connection():
        msg = f"Cannot reach Emby server at:\n{s_url}"
        logger.critical(f"Connection Error: {msg}")
        QMessageBox.critical(None, "Connection Error", msg)
        sys.exit(1)
    if not api.authenticate():
        msg = "Authentication failed.\nCheck username and password."
        logger.critical(f"Auth Error: {msg}")
        QMessageBox.critical(None, "Auth Error", msg)
        sys.exit(1)

    if cfg.getboolean("Settings", "cleanup_on_startup", fallback=False):
        dlg = QDialog(); dlg.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        dlg.setStyleSheet(
            f"background: {BG_DEEP}; border: 2px solid {CYAN};"
            f" border-radius: 8px;"
        )
        dlg.setMinimumWidth(380)
        dl = QVBoxLayout(dlg); dl.setContentsMargins(32, 26, 32, 26)
        lbl = QLabel("SYSTEM MAINTENANCE\n◈  PURGING TAGGED ITEMS  ◈")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(
            f"color: {CYAN}; font-weight: 900; font-size: 14px;"
            f" font-family: 'Consolas', 'Cascadia Code', 'Segoe UI', monospace;"
            f" letter-spacing: 2px; background: transparent;"
        )
        dl.addWidget(lbl)
        t = QThread(); w = CleanupWorker(api); w.moveToThread(t)
        w.progress.connect(lambda name: lbl.setText(f"PURGING:\n{name[:42]}"))
        w.finished.connect(lambda ok, fail: (
            logger.info("Maintenance: %d deleted, %d failed.", ok, fail),
            t.quit(), dlg.accept()))
        t.started.connect(w.run); t.start()
        dlg.exec(); t.wait()

    try:
        r = api.get(f"/Users/{api.user_id}/Views", timeout=10)
        libs = sorted(v["Name"] for v in r.json().get("Items", []))
    except requests.exceptions.RequestException as e:
        logger.error("Failed to fetch user views from Emby API: %s", e)
        libs = []
    except Exception as e:
        logger.error("An unexpected error occurred while fetching user views: %s", e)
        libs = []

    # Order matches Windows Display Settings (Monitor 1, 2, ... left-to-right)
    ordered_screens = _ordered_screens(app)
    wiz = SetupWizard(cfg, ordered_screens, libs)
    if wiz.exec() != QDialog.DialogCode.Accepted:
        api.close(); sys.exit(0)

    s = wiz.get_settings()
    if not s["screens"] or not s["libraries"]:
        msg = "Select at least one display and one library."
        logger.warning(f"Setup Error: {msg}")
        QMessageBox.warning(None, "Setup Error", msg)
        api.close(); sys.exit(1)

    if not cfg.has_section("Settings"):
        cfg.add_section("Settings")
    cfg.set("Settings", "last_screens",   ",".join(x.name() for x in s["screens"]))
    cfg.set("Settings", "last_libraries", ",".join(s["libraries"]))
    cfg.set("Settings", "last_grid_rows", str(s["grid"][0]))
    cfg.set("Settings", "last_grid_cols", str(s["grid"][1]))
    with open(CONFIG_FILE, "w") as f:
        cfg.write(f)

    logger.info("Initializing HyperWall 8.1 structured package…")
    _eff = apply_perf_env(MPV_OPTS)
    logger.info(
        "Perf: vo=%s gpu_api=%s hwdec=%s profile=%s video_sync=%s hdr_hint=%s stats=%s",
        _eff.get("vo"), _eff.get("gpu_api"), _eff.get("hwdec"),
        _eff.get("profile"), _eff.get("video_sync"),
        _eff.get("target_colorspace_hint"), "on" if STATS_ENABLED else "off",
    )
    wall = WallController(s, api)
    app.aboutToQuit.connect(wall._cleanup)
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
