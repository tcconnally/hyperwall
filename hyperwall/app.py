"""
Hyperwall — application bootstrap and main().

Orchestrates startup: DLL registration, NVIDIA profile, config loading,
Emby authentication, wizard, wall launch, and web remote.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import socket
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
from .config import HyperwallConfig, ConfigMissingError, effective_server_url
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
from .backends import resolve_backend
from .nvidia import ensure_nvidia_profile, maybe_relaunch_in_isolation
from . import theme
from .wizard import SetupWizard
from .wall import WallController, MouseIdleHider
from .sync import SyncServer, SyncClient, DEFAULT_SYNC_PORT

logger = logging.getLogger("HyperWall")

_WEB_AVAILABLE = False
try:
    from . import web as _web
    _WEB_AVAILABLE = os.environ.get("HYPERWALL_WEB") == "1"
except ImportError:
    pass


# ── mpv DLL registration ─────────────────────────────────────────────────────
# Must happen once, before any mpv import. Every cookie must stay alive
# (held at module level) to prevent GC from removing its DLL directory —
# a single variable here would drop all but the last directory registered.

_mpv_dll_cookies: list = []

if os.name == "nt":
    _dll_dirs = [SCRIPT_DIR]
    if getattr(sys, "frozen", False):
        _dll_dirs.insert(0, sys._MEIPASS)

    for _d in _dll_dirs:
        if os.path.isdir(_d):
            try:
                _mpv_dll_cookies.append(os.add_dll_directory(_d))
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

    # 0. Optional headless sync-relay mode (no Qt, no mpv).
    parser = argparse.ArgumentParser(prog="hyperwall")
    parser.add_argument(
        "--sync-relay",
        action="store_true",
        help="Run only the headless sync relay (no GUI).",
    )
    parser.add_argument(
        "--sync-host",
        default="0.0.0.0",
        help="Host to bind the sync relay to (default: 0.0.0.0).",
    )
    parser.add_argument(
        "--sync-port",
        type=int,
        default=DEFAULT_SYNC_PORT,
        help=f"Port for the sync relay (default: {DEFAULT_SYNC_PORT}).",
    )
    args = parser.parse_args()

    if args.sync_relay:
        from .sync import run_sync_relay
        run_sync_relay(host=args.sync_host, port=args.sync_port)
        return

    # 1. NVIDIA isolation: re-exec into bundled exe if needed
    maybe_relaunch_in_isolation()

    # 2. Verify libmpv is importable
    try:
        import mpv  # noqa: F401
    except Exception as e:
        if sys.platform == "darwin":
            hint = (
                "Install libmpv via Homebrew:\n  brew install mpv\n\n"
                "Then launch via ./launch.sh — it exports\n"
                "DYLD_FALLBACK_LIBRARY_PATH so python-mpv finds\n"
                "/opt/homebrew/lib/libmpv.dylib on Apple Silicon."
            )
        elif os.name == "nt":
            hint = (
                f"And place mpv-2.dll next to this script:\n  {SCRIPT_DIR}\n\n"
                f"Download: https://sourceforge.net/projects/mpv-player-windows/files/libmpv/\n"
                f"  (shinchiro build — extract libmpv-2.dll, place in script dir)"
            )
        else:
            hint = "Install libmpv via your distro (mpv-libs / libmpv-dev)."
        msg = (
            f"python-mpv failed to load: {e}\n\n"
            f"Install:\n  pip install python-mpv\n\n{hint}"
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

    # 3b. libmpv hard-fails mpv_create() (returns NULL → python-mpv then
    # segfaults dereferencing it in mpv_set_option) when LC_NUMERIC is not
    # "C"/"C.UTF-8" (mpv player/main.c check_locale). CPython calls
    # setlocale(LC_ALL, "") at startup on POSIX, so a normal en_US.UTF-8
    # macOS shell kills every libmpv embed at first MPV() — the Windows CRT
    # keeps LC_NUMERIC=C, which is why this only bites POSIX.
    if os.name != "nt":
        import locale as _locale
        try:
            _locale.setlocale(_locale.LC_NUMERIC, "C")
        except _locale.Error as e:
            logger.warning("Could not force LC_NUMERIC=C: %s", e)

    # 4. Process priority (HIGH)
    if os.name == "nt" and not os.environ.get("HYPERWALL_NO_LOG_SETUP"):
        try:
            # Explicit 64-bit handle types: with ctypes' default c_int
            # restype the pseudo-handle truncates and SetPriorityClass
            # fails silently (returns 0) — this call was a no-op from v9
            # through v10.9 while logging success (2026-07-13 audit).
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k32.GetPriorityClass.argtypes = [ctypes.c_void_p]
            ok = k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080)
            got = k32.GetPriorityClass(k32.GetCurrentProcess())
            if ok and got == 0x00000080:
                logger.info("Kernel: Priority set to HIGH (verified).")
            else:
                logger.warning(
                    "Kernel: HIGH priority NOT applied "
                    "(SetPriorityClass=%s, class=%#x).", ok, got,
                )
        except Exception as e:
            logger.warning("Kernel: priority change failed: %s", e)

    if sys.platform == "darwin":
        # macOS cells render through QOpenGLWidget (macembed.py): default a
        # 3.2 core-profile context (resolves to 4.1 on Apple Silicon) before
        # any GL context can be created.
        from PyQt6.QtGui import QSurfaceFormat
        _fmt = QSurfaceFormat()
        _fmt.setVersion(3, 2)
        _fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        _fmt.setDepthBufferSize(0)  # 2D video only
        QSurfaceFormat.setDefaultFormat(_fmt)

    app = QApplication(sys.argv)
    theme.apply(app)

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
    server_url = effective_server_url(
        cfg.server_url, os.environ.get("HYPERWALL_SERVER_URL")
    )
    if server_url != cfg.server_url:
        logger.info("Endpoint override active for this launch: %s", server_url)
    client = EmbyClient(
        server_url, cfg.username, cfg.password, verify_ssl=cfg.verify_ssl,
        backend=resolve_backend(cfg.backend),
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
            f"Cannot reach Emby server at:\n{server_url}",
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
        last_preview_rows=cfg.last_preview_rows,
        last_preview_cols=cfg.last_preview_cols,
        last_display_roles=cfg.display_roles(),
        last_display_layouts=cfg.display_layouts(),
        last_display_settings=cfg.display_settings(),
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
        backend=cfg.backend,
        last_screens=",".join(s.name() for s in settings["screens"]),
        last_libraries=",".join(settings["libraries"]),
        last_grid_rows=settings["grid_rows"],
        last_grid_cols=settings["grid_cols"],
        last_preview_rows=settings["preview_rows"],
        last_preview_cols=settings["preview_cols"],
        last_display_roles=json.dumps(settings["display_roles"]),
        last_display_layouts=json.dumps(
            settings.get("display_layouts", {}), sort_keys=True
        ),
        last_display_settings=json.dumps(
            settings.get("display_settings", {}), sort_keys=True
        ),
        cleanup_on_startup=cfg.cleanup_on_startup,
        sync_enabled=cfg.sync_enabled,
        sync_server=cfg.sync_server,
        sync_host=cfg.sync_host,
        sync_port=cfg.sync_port,
        sync_display_name=cfg.sync_display_name,
        scenes=cfg.scenes,  # preserve saved scene presets across the rewrite
    )
    # Diagnostic phases use the wizard for temporary runtime settings but must
    # not rewrite the operator's persistent config.ini.
    if os.environ.get("HYPERWALL_NO_CONFIG_SAVE") != "1":
        cfg.save()
    else:
        logger.info("Config persistence disabled for this soak phase.")

    # 11. Perf env
    _eff = apply_env_overrides(MPV_OPTS)
    _render_profile = os.environ.get(
        "HYPERWALL_RENDER_PROFILE",
        "hq" if sys.platform == "darwin" else "platform-default",
    )
    logger.info(
        "Perf: vo=%s gpu_api=%s hwdec=%s profile=%s render_profile=%s video_sync=%s "
        "hdr_hint=%s stats=%s",
        _eff.get("vo"), _eff.get("gpu_api"), _eff.get("hwdec"),
        _eff.get("profile"), _render_profile, _eff.get("video_sync"),
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
        display_roles=settings.get("display_roles"),
        display_layouts=settings.get("display_layouts"),
        preview_rows=settings.get("preview_rows", 3),
        preview_cols=settings.get("preview_cols", 4),
    )

    # Optional network sync layer for multi-machine walls.
    if cfg.sync_enabled:
        if cfg.sync_server:
            sync = SyncServer(wall, host=cfg.sync_host, port=cfg.sync_port)
        else:
            # For clients, connect to the server's IP (not 0.0.0.0).
            host = cfg.sync_host if cfg.sync_host != "0.0.0.0" else "127.0.0.1"
            sync = SyncClient(
                wall,
                host=host,
                port=cfg.sync_port,
                display_name=cfg.sync_display_name or socket.gethostname(),
            )
        sync.start()
        wall.set_sync_adapter(sync)
        logger.info(
            "Sync %s started (%s:%d) as %s",
            "server" if cfg.sync_server else "client",
            cfg.sync_host,
            cfg.sync_port,
            cfg.sync_display_name or socket.gethostname(),
        )

    if _WEB_AVAILABLE:
        _web.start(wall)
    elif "_web" in globals():
        # flask IS bundled (static import chain) — the real gate is the env
        # var. The old message blamed "flask not installed" and sent
        # debugging down the wrong path (2026-07-13 audit).
        logger.info("Web remote off (set HYPERWALL_WEB=1 to enable).")
    else:
        logger.info("Web remote unavailable (flask not importable).")

    # NOTE: WallController is a plain object, not a QObject — it must not be
    # used as a Qt parent (crashed the 10.6.0 soak launch). These live on
    # locals until app.exec() returns.
    from .perftrace import PERFTRACE_ENABLED, LoopLagSampler
    if PERFTRACE_ENABLED:
        _lag_sampler = LoopLagSampler()
        _lag_sampler.start()

    from .soak import SOAK_MINUTES, SoakController
    if SOAK_MINUTES > 0:
        _soak = SoakController(wall)

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
