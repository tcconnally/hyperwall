import os
import sys
import ctypes
import logging
import configparser
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

# Late import for mpv
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

def main():
    sys.excepthook = _handle_exception

    # 1. NVIDIA isolation: re-exec into bundled launcher if present.
    maybe_relaunch_in_isolation(LAUNCH_BASENAME, LAUNCHER_EXE, SCRIPT_DIR)

    # 2. libmpv presence check
    if mpv is None:
        msg = (f"python-mpv failed to load: {_MPV_IMPORT_ERR}\n\n"
               f"Install:\n  pip install python-mpv\n\n"
               f"And place mpv-2.dll next to this script:\n  {SCRIPT_DIR}\n\n"
               f"Download: https://mpv.io/installation/  (use shobon-mpv builds)")
        try:
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "HyperWall — libmpv missing", msg)
        except Exception:
            print(msg, file=sys.stderr)
        sys.exit(1)

    # 3. Setup logging
    setup_logging(LOG_FILE)

    # 4. Set priority
    if not os.environ.get("HYPERWALL_NO_LOG_SETUP"):
        try:
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080  # HIGH
            )
            logger.info("Kernel: Priority set to HIGH.")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

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
        QMessageBox.information(
            None, "Config Created",
            f"config.ini created at:\n{os.path.abspath(CONFIG_FILE)}\n\n"
            "Fill in Emby server URL, username, password, then restart.",
        )
        sys.exit(0)

    cfg.read(CONFIG_FILE)
    s_url  = cfg.get("Login", "server_url", fallback="")
    s_user = cfg.get("Login", "username",   fallback="")
    s_pass = cfg.get("Login", "password",   fallback="")
    if not s_url or not s_user:
        QMessageBox.critical(None, "Config Error",
                             "server_url and username must be set in config.ini.")
        sys.exit(1)

    api = EmbyAPISession(s_url, s_user, s_pass)
    if not api.test_connection():
        QMessageBox.critical(None, "Connection Error",
                             f"Cannot reach Emby server at:\n{s_url}")
        sys.exit(1)
    if not api.authenticate():
        QMessageBox.critical(None, "Auth Error",
                             "Authentication failed.\nCheck username and password.")
        sys.exit(1)

    if cfg.getboolean("Settings", "cleanup_on_startup", fallback=False):
        dlg = QDialog(); dlg.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        dlg.setStyleSheet("background: #111; border: 1px solid #2a2a2a;")
        dlg.setMinimumWidth(340)
        dl = QVBoxLayout(dlg); dl.setContentsMargins(28, 22, 28, 22)
        lbl = QLabel("SYSTEM MAINTENANCE\nPurging tagged items…")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("color: #3b8edb; font-weight: bold; font-size: 13px;"
                          " font-family: 'Segoe UI'; background: transparent;")
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
    except Exception:
        libs = []

    wiz = SetupWizard(cfg, app.screens(), libs)
    if wiz.exec() != QDialog.DialogCode.Accepted:
        api.close(); sys.exit(0)

    s = wiz.get_settings()
    if not s["screens"] or not s["libraries"]:
        QMessageBox.warning(None, "Setup Error",
                            "Select at least one display and one library.")
        api.close(); sys.exit(1)

    if not cfg.has_section("Settings"):
        cfg.add_section("Settings")
    cfg.set("Settings", "last_screens",   ",".join(x.name() for x in s["screens"]))
    cfg.set("Settings", "last_libraries", ",".join(s["libraries"]))
    cfg.set("Settings", "last_grid_rows", str(s["grid"][0]))
    cfg.set("Settings", "last_grid_cols", str(s["grid"][1]))
    with open(CONFIG_FILE, "w") as f:
        cfg.write(f)

    logger.info("Initializing HyperWall 8.0…")
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
