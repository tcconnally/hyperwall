"""
PROJECT: HYPERWALL
VERSION: 8.0
AUTHOR:  Thomas Connally
DATE:    May 2026

Backend: python-mpv (libmpv). Replaces 7.4's Qt6/ffmpeg/D3D stack — see brief.
Closed-LAN, single-user, Emby-only, Windows-only. Hardware target:
  PC : RTX 5070 Ti, Ryzen 7 9800X3D, 32 GB RAM, 2.5 GbE
  LGs: 27GS95UE-B (4K 240 Hz OLED) ×2 + LG C5 (occasional 3rd display)
  greg: i5-13500 + QSV, Emby always-DIRECT static file serve (no transcoder)

G-Sync isolation: when launched as the bundled hyperwall_v8.exe (PyInstaller),
the NVIDIA driver matches only this process and applies hyperwall_v8.nip
(VRR off, Power = Prefer Max Performance). Sentinel-tracked so a driver
reinstall triggers a single UAC reapply — daily launches stay quiet.
"""

from __future__ import annotations

import ctypes
import configparser
import logging
import os
import random
import subprocess
import sys
import threading
import uuid
from collections import deque
from logging.handlers import RotatingFileHandler

# ── PyQt6 ─────────────────────────────────────────────────────────────────────
try:
    from PyQt6.QtCore import (
        Qt, QEvent, QObject, QThread, QTimer,
        QPropertyAnimation, QEasingCurve, pyqtSignal, pyqtSlot,
    )
    from PyQt6.QtGui import QShortcut, QKeySequence
    from PyQt6.QtWidgets import (
        QApplication, QDialog, QFrame, QGraphicsOpacityEffect, QGridLayout,
        QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
        QMainWindow, QMessageBox, QPushButton, QSlider, QSpinBox, QStyle,
        QVBoxLayout, QWidget,
    )
except ImportError as e:
    print(f"FATAL: PyQt6 not installed — {e}")
    sys.exit(1)

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:
    print("FATAL: requests not installed")
    sys.exit(1)

# Optional companion module — drives trio_remix_mv.py on greg via ssh.
# Failure to import is non-fatal: the R shortcut just becomes a no-op then.
try:
    from hyperwall_remix import remix_walls as _remix_walls
except ImportError as _e:
    _remix_walls = None
    print(f"NOTE: hyperwall_remix unavailable ({_e}); 'R' shortcut disabled.")

# ── Paths ─────────────────────────────────────────────────────────────────────
# When frozen by PyInstaller, sys.executable is hyperwall_v8.exe (the launcher
# we want NVIDIA's driver to match). When run as .py, __file__ is the script.
# Sentinel/config/log live next to whichever is the launch artifact so an exe
# build and a script run can share state cleanly.
#
# IMPORTANT: SCRIPT_DIR computation + the PATH prepend below MUST happen
# BEFORE `import mpv`. python-mpv loads mpv-2.dll via ctypes.CDLL at module
# import time, and Python 3.8+ on Windows disabled cwd-based DLL search —
# so an absolute PATH entry pointing at the DLL is the only reliable lookup
# (find_library + relative-path fallback in the python-mpv loader fails
# with the "find_library found mpv.dll under a relative path entry"
# diagnostic otherwise).
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
    LAUNCH_BASENAME = os.path.basename(sys.executable).lower()
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    LAUNCH_BASENAME = os.path.basename(sys.executable).lower()

CONFIG_FILE       = os.path.join(SCRIPT_DIR, "config.ini")
LOG_FILE          = os.path.join(SCRIPT_DIR, "hyperwall.log")
LAUNCHER_EXE      = os.path.join(SCRIPT_DIR, "hyperwall_v8.exe")
NIP_FILE          = os.path.join(SCRIPT_DIR, "hyperwall_v8.nip")
NPI_EXE           = os.path.join(SCRIPT_DIR, "tools", "nvidiaProfileInspector.exe")
NV_SENTINEL       = os.path.join(SCRIPT_DIR, ".hyperwall_v8_nvprofile.sentinel")

os.environ["PATH"] = SCRIPT_DIR + os.pathsep + os.environ.get("PATH", "")

# python-mpv loads libmpv lazily on MPV() construction — defer import-time
# error to a friendlier check in main() so we can surface the DLL-missing case.
try:
    import mpv  # type: ignore
    _MPV_IMPORT_ERR: Exception | None = None
except Exception as e:
    mpv = None  # type: ignore
    _MPV_IMPORT_ERR = e


# ── Logging ───────────────────────────────────────────────────────────────────
# File handler gated by env var so test harnesses can `import hyperwall_v8` to
# share MPV_OPTS without their runs writing into hyperwall.log.
logger = logging.getLogger("HyperWall")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")
if not os.environ.get("HYPERWALL_NO_LOG_SETUP"):
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    _fh.setFormatter(_fmt)
    logger.addHandler(_fh)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
logger.addHandler(_ch)


def _handle_exception(et, ev, tb):
    if issubclass(et, KeyboardInterrupt):
        sys.__excepthook__(et, ev, tb)
        return
    logger.critical("UNHANDLED EXCEPTION", exc_info=(et, ev, tb))


sys.excepthook = _handle_exception


# ── Process priority ──────────────────────────────────────────────────────────
# Skip when imported by a test harness (test process picks its own priority).
if not os.environ.get("HYPERWALL_NO_LOG_SETUP"):
    try:
        ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), 0x00000080  # HIGH
        )
        logger.info("Kernel: Priority set to HIGH.")
    except Exception:
        pass


# ── Tuning constants (per brief) ──────────────────────────────────────────────
STREAM_START_STAGGER_MS = 300       # ms between cell starts
MAX_RETRIES             = 3         # then skip the dead stream
CONTROLS_HEIGHT         = 36        # px
CONTROLS_OPACITY        = 0.65
AUTOHIDE_MS             = 5_000     # one-shot startup auto-hide
OVERLAY_SHOW_MS         = 3_000     # title overlay before fade
MOUSE_IDLE_MS           = 3_000     # cursor auto-hide

# mpv tuning — locked to the hardware spec above.
# hwdec='nvdec-copy': forces NVDEC explicitly (was 'auto-copy'). Phase 2 stats
# runs 2026-05-08 caught auto-copy's fallback chain hitting d3d11va context
# exhaustion at staggered startup ("Failed setup for format d3d11" → falls
# through dxva2_vld → software decode for the affected cell), causing visible
# stutter on heavy 60fps content (f3v0r_gimme_1, goon_loop_bang_1) for the
# first ~30s until recreate-per-play landed on a working hwdec. NVDEC has no
# concurrent-session limit on Blackwell — going direct eliminates the fallback
# storm. Override via HYPERWALL_HWDEC if needed.
#
# profile='fast': bilinear scaling, no Lanczos/sigmoid/tone-mapping shaders.
# gpu-hq at 12 concurrent cells stacks expensive shader work on a single GPU and
# was the primary cause of "performance is shite". Quality difference is zero at
# wall-viewing distance on a grid of small cells. mpv itself logs "Consider trying
# --profile=fast" when AV desync spills out of the tolerance window — we listened.
#
# video_sync='audio': sync video frames to the audio clock. display-resample ties
# every instance to the 240Hz display timer (resamples audio/video each frame to
# hit display PTS) — at 12 concurrent cells that's 12x the resampling math per
# vsync. audio-clock sync is free and indistinguishable on a wall.
#
# readahead trimmed: 20s x 12 cells = 240s of buffered data at once. 5s is
# ample for a 2.5 GbE LAN source and cuts peak RAM and greg I/O load.
MPV_OPTS = dict(
    vo                         = "gpu-next",
    gpu_api                    = "d3d11",
    hwdec                      = "nvdec-copy",
    profile                    = "fast",
    video_sync                 = "audio",
    interpolation              = "no",
    # target_colorspace_hint=no: HDR hint forced gpu-next's tone-mapping pass
    # on every cell every frame, even for SDR sources (which is most of the
    # library per the codec matrix). At 8 cells this was the dominant render
    # cost — turning it off was the change that finally killed the per-file
    # stutter (top_creators_remix_hot_asf_1, etc.) in 2026-05-08 stats runs.
    # Cost: SDR sources display without the gpu-next HDR-aware path on the
    # 4K HDR OLED — visually indistinguishable at wall viewing distance.
    target_colorspace_hint     = "no",
    cache                      = "yes",
    cache_secs                 = 5,
    demuxer_max_bytes          = "128MiB",
    demuxer_readahead_secs     = 5,
    network_timeout            = 15,
    stream_lavf_o              = "reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
    keep_open                  = "no",
    osd_level                  = 0,
    input_default_bindings     = False,
    input_vo_keyboard          = False,
    ytdl                       = False,
    # ao=wasapi only -- no null fallback. Earlier audio_fallback_to_null
    # silenced the "no playback devices" startup noise but permanently
    # routed audio to the null sink, which made unmute land on a sink with
    # no output. Cell-level WASAPI failure now degrades to silent video
    # for that cell instead of breaking unmute everywhere.
    ao                         = "wasapi",
    audio_client_name          = "HyperWall",
    # Audio buffer raised from mpv default 0.2s to 1.0s. With 8 cells each
    # holding a WASAPI shared-mode sink, the Windows audio mixer was
    # underrunning every ~5s during a stats run on 2026-05-07; underruns
    # force video_sync=audio to re-sync, producing visible stutter even
    # though render-side counters were clean. Latency cost is irrelevant
    # for a wall — nobody cares about A/V <1s latency. Override via
    # HYPERWALL_AUDIO_BUFFER if the underruns return at this size.
    audio_buffer               = 1.0,
    msg_level                  = "all=warn,cplayer=info,ao=error,ao/wasapi=fatal",
)


# ── Phase 2 perf instrumentation + env-driven A/B knobs ──────────────────────
# Wall is exclusive to one PC + one Emby; tweaks tuned for RTX 5070 Ti +
# LG 27GS95UE-B 240Hz HDR OLED + Blackwell NVDEC. Defaults preserve existing
# production behavior — env vars opt into A/B variations.
#
#   HYPERWALL_STATS=1               Per-cell render stats observers on
#                                   frame-drop-count, mistimed-frame-count,
#                                   vo-delayed-frame-count, decoder drops.
#                                   Aggregated across mpv recreate-per-play
#                                   and dumped to JSON + log on shutdown.
#   HYPERWALL_HDR_HINT=0|1          Override target_colorspace_hint.
#   HYPERWALL_HWDEC=<value>         Override hwdec (auto-copy|d3d11va|nvdec|...).
#   HYPERWALL_GPU_API=d3d11|vulkan  Override gpu_api.
#   HYPERWALL_PROFILE=fast|gpu-hq   Override mpv profile.
#   HYPERWALL_VIDEO_SYNC=<value>    Override video_sync.
STATS_ENABLED = os.environ.get("HYPERWALL_STATS") == "1"

# mpv counter properties — monotonic per instance, reset on recreate.
# We snapshot before destroy and accumulate into per-cell totals.
STATS_COUNTER_PROPS = (
    "frame-drop-count",         # vo-side presentation drops
    "mistimed-frame-count",     # frames presented at wrong time
    "vo-delayed-frame-count",   # vo couldn't keep up with decode
    "decoder-frame-drop-count", # lavc decode-side drops
)

# mpv informational properties — latest-value-wins. Useful to know what hwdec
# actually got selected, what fps the source is, etc.
STATS_INFO_PROPS = (
    "hwdec-current",
    "video-bitrate",
    "container-fps",
    "estimated-vf-fps",
)


def _apply_perf_env(opts: dict) -> dict:
    """Return a copy of opts with HYPERWALL_* env-var overrides applied.
    Keep the merge point at construction time so a single instance can be
    re-created with different settings without restarting the wall."""
    out = dict(opts)
    if (v := os.environ.get("HYPERWALL_HDR_HINT")) is not None:
        out["target_colorspace_hint"] = "yes" if v == "1" else "no"
    if (v := os.environ.get("HYPERWALL_AUDIO_BUFFER")) is not None:
        try:
            out["audio_buffer"] = float(v)
        except ValueError:
            pass
    for var, key in (
        ("HYPERWALL_VO",          "vo"),
        ("HYPERWALL_HWDEC",       "hwdec"),
        ("HYPERWALL_GPU_API",     "gpu_api"),
        ("HYPERWALL_PROFILE",     "profile"),
        ("HYPERWALL_VIDEO_SYNC",  "video_sync"),
    ):
        if (v := os.environ.get(var)) is not None:
            out[key] = v
    return out


# Known-benign mpv warnings that spam our log on rotation through varied
# library content. Filtered at log_handler — mpv still emits them, we just
# don't escalate to the Python logger.
_MPV_LOG_NOISE = (
    "UDTA parsing failed retrying raw",
    "Detected creation time before 1970",
    "Unknown cover type",
    "stream 0, timescale not set",
    "client removed during hook handling",
)


# ── Hybrid URL routing: DIRECT for ≤1080p@30, TRANSCODE for heavier ───────────
# Phase 2 audit 2026-05-08 found that 4K@60 sources contend for the wall PC's
# GPU 3D engine at 8-cell grid sizes — even SDR 1080p cells stutter when one
# heavy 4K@60 cell is active because mpv's gpu-next render path saturates.
# greg's i5-13500 QSV is otherwise idle on always-DIRECT.
#
# Pivot: hybrid routing. Light sources stay DIRECT (decode locally on NVDEC,
# the path that proved clean at 12-cell typical-load stress). Heavy sources
# route through Emby's QSV transcoder to 1080p@30 H.264, which makes them
# trivial for the wall PC to render. Per-cell render cost goes ~8x lower for
# 4K@60 sources.
#
# 2026-05-03's "always-REMUX is broken" pivot was driven by 9-concurrent 4K
# QSV transcodes overwhelming the i5-13500. The hybrid path here only sends
# the small minority of heavy sources to transcode — typically 0-2 concurrent,
# well within QSV capacity.
#
# Override via HYPERWALL_AUTO_TRANSCODE=0 to revert to pure DIRECT.
_AUTO_TRANSCODE = os.environ.get("HYPERWALL_AUTO_TRANSCODE", "1") == "1"


def _needs_transcode(item: dict) -> bool:
    """Inspect Emby item metadata: should this source go through Emby's QSV
    transcoder rather than DIRECT? True when source resolution exceeds 1080p
    (1440p/4K). Returns False on missing metadata — when in doubt, DIRECT.

    Initial framing also triggered on framerate > 30, but smoke-testing the
    real library 2026-05-08 showed that catches ~53% of items (most PMV/
    TikTok content is 60fps even at 1080p) and would push concurrent QSV
    transcodes near the 9-cell ceiling that broke the original always-REMUX
    pivot. 1080p@60 is ~4x lighter on the wall PC than 4K and was never
    observed as problematic in user reports — leaving it DIRECT."""
    if not _AUTO_TRANSCODE:
        return False
    src = (item.get("MediaSources") or [{}])[0]
    streams = src.get("MediaStreams") or item.get("MediaStreams") or []
    v = next((s for s in streams if s.get("Type") == "Video"), {}) or {}
    w = v.get("Width") or 0
    h = v.get("Height") or 0
    return w > 1920 or h > 1080


# ── Stylesheet ────────────────────────────────────────────────────────────────
CTRL_STYLE = """
    QFrame#controls {
        background: rgba(55, 55, 55, 220);
        border-top: 1px solid rgba(255, 255, 255, 18);
    }
    QLabel { color: #ccc; font-family: 'Segoe UI'; font-size: 9px; background: transparent; }
    QPushButton {
        background: rgba(80, 80, 80, 180); border: 1px solid rgba(255,255,255,20);
        border-radius: 2px; color: #eee; font-size: 11px; padding: 1px;
        min-width: 22px; min-height: 22px; max-width: 22px; max-height: 22px;
    }
    QPushButton:hover   { background: #2563a8; border-color: #3b8edb; color: white; }
    QPushButton:checked { background: #1e4f78; border-color: #3b8edb; color: white; }
    QSlider::groove:horizontal { background: rgba(100,100,100,180); height: 3px; border-radius: 1px; }
    QSlider::sub-page:horizontal { background: rgba(59,142,219,200); border-radius: 1px; }
    QSlider::handle:horizontal {
        background: rgba(220,220,220,220); width: 8px; margin: -2px 0; border-radius: 4px;
    }
"""


# ==============================================================================
# 1. NVIDIA PROFILE ISOLATION (G-Sync off only when running as our exe)
# ==============================================================================
def _nv_driver_version() -> str | None:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
            text=True, timeout=5, creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        return out.strip().splitlines()[0]
    except Exception:
        return None


def ensure_nvidia_profile() -> None:
    """Apply the HyperWall NVIDIA profile if the driver was (re)installed since
    we last applied. Sentinel = current driver version; mismatch = reapply.
    Only meaningful when running as hyperwall_v8.exe (the profile targets that
    basename); script-mode logs a warning and skips."""
    if LAUNCH_BASENAME != "hyperwall_v8.exe":
        logger.warning(
            "G-Sync isolation disabled — running as '%s', not hyperwall_v8.exe. "
            "Build via build_v8.bat for full isolation.", LAUNCH_BASENAME,
        )
        return
    if not os.path.exists(NIP_FILE):
        logger.warning("Missing NVIDIA profile %s — isolation skipped.", NIP_FILE)
        return
    if not os.path.exists(NPI_EXE):
        logger.warning("Missing %s — install nvidiaProfileInspector to enable isolation.", NPI_EXE)
        return

    drv = _nv_driver_version()
    if not drv:
        logger.warning("Could not read NVIDIA driver version — skipping profile check.")
        return

    if os.path.exists(NV_SENTINEL):
        try:
            with open(NV_SENTINEL, encoding="utf-8") as f:
                if f.read().strip() == drv:
                    logger.info("NVIDIA profile current (driver %s).", drv)
                    return
        except Exception:
            pass

    logger.info("Applying NVIDIA profile (driver %s) — UAC elevation required.", drv)
    # ShellExecute with 'runas' verb triggers UAC; -silentImport is NPI's
    # headless-apply flag.
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", NPI_EXE,
            f'-silentImport "{NIP_FILE}"', SCRIPT_DIR, 0,  # SW_HIDE
        )
        if rc <= 32:
            logger.warning("ShellExecuteW returned %d — NPI did not launch.", rc)
            return
        # Best-effort: write sentinel even though we can't confirm import succeeded
        # synchronously (NPI is async). On failure, we'll re-prompt next launch.
        with open(NV_SENTINEL, "w", encoding="utf-8") as f:
            f.write(drv)
        logger.info("NVIDIA profile applied; sentinel written.")
    except Exception as e:
        logger.warning("Failed to apply NVIDIA profile: %s", e)


def maybe_relaunch_in_isolation() -> None:
    """If user ran `python hyperwall_v8.py` and the bundled launcher exists,
    re-exec into it so NVIDIA's driver matches the isolated exe name. Silent
    no-op when already isolated, or when no launcher is built.

    HYPERWALL_NO_RELAUNCH=1 bypasses the re-exec — useful when iterating on
    script changes without rebuilding the bundle. Loses G-Sync isolation for
    that session; the next normal launch picks it back up."""
    if LAUNCH_BASENAME == "hyperwall_v8.exe":
        return
    if not os.path.exists(LAUNCHER_EXE):
        return
    if os.environ.get("HYPERWALL_NO_RELAUNCH") == "1":
        logger.info("Re-launch suppressed (HYPERWALL_NO_RELAUNCH=1) — script mode, no isolation.")
        return
    logger.info("Re-launching via isolated exe: %s", LAUNCHER_EXE)
    try:
        # CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS so the new console is clean
        subprocess.Popen([LAUNCHER_EXE] + sys.argv[1:], cwd=SCRIPT_DIR, close_fds=True)
        sys.exit(0)
    except Exception as e:
        logger.warning("Re-launch failed (%s) — continuing in current process.", e)


# ==============================================================================
# 2. EMBY API SESSION
# ==============================================================================
class EmbyAPISession:
    """HTTP-only Emby client. Lock guards auth state; requests.Session is
    thread-safe for concurrent reads, so get/post/delete run lock-free."""

    def __init__(self, server_url: str, username: str, password: str):
        self.server_url = server_url.rstrip("/")
        self.username   = username
        self._password  = password
        self.access_token: str | None = None
        self.user_id: str | None      = None
        self._auth_lock = threading.Lock()
        self._device_id = f"hyperwall-{os.urandom(4).hex()}"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":      "HyperWall/8.0",
            "Accept":          "application/json",
            "Accept-Encoding": "gzip, deflate",
        })

    def test_connection(self) -> bool:
        try:
            r = self.session.get(f"{self.server_url}/System/Info/Public",
                                 timeout=5, verify=False)
            return r.status_code == 200
        except Exception:
            return False

    def authenticate(self) -> bool:
        with self._auth_lock:
            try:
                r = self.session.post(
                    f"{self.server_url}/Users/AuthenticateByName",
                    headers={
                        "Content-Type": "application/json",
                        "X-Emby-Authorization": (
                            f'MediaBrowser Client="HyperWall", Device="PC", '
                            f'DeviceId="{self._device_id}", Version="8.0"'
                        ),
                    },
                    json={"Username": self.username, "Pw": self._password},
                    timeout=10, verify=False,
                )
                r.raise_for_status()
                d = r.json()
                self.access_token = d.get("AccessToken")
                self.user_id      = d.get("User", {}).get("Id")
                logger.info("Authenticated. User ID: %s", self.user_id)
                return bool(self.access_token and self.user_id)
            except Exception as e:
                logger.error("Authentication error: %s", e)
                return False

    def _h(self) -> dict: return {"X-Emby-Token": self.access_token}

    def get(self, path: str, **kw):
        return self.session.get(f"{self.server_url}{path}", headers=self._h(), verify=False, **kw)

    def post(self, path: str, **kw):
        return self.session.post(f"{self.server_url}{path}", headers=self._h(), verify=False, **kw)

    def delete(self, path: str, **kw):
        return self.session.delete(f"{self.server_url}{path}", headers=self._h(), verify=False, **kw)

    def close(self): self.session.close()


# ==============================================================================
# 3. BACKGROUND WORKERS
# ==============================================================================
class CleanupWorker(QObject):
    finished = pyqtSignal(int, int)
    progress = pyqtSignal(str)

    def __init__(self, api: EmbyAPISession):
        super().__init__()
        self.api = api
        self._cancelled = False

    @pyqtSlot()
    def run(self):
        logger.info("Maintenance: Starting cleanup...")
        try:
            r = self.api.get(
                f"/Users/{self.api.user_id}/Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Video,MusicVideo,Movie,Episode",
                    "Tags": "ToDelete",
                    "Limit": "500",
                }, timeout=10,
            )
            items = r.json().get("Items", [])
            if not items:
                self.finished.emit(0, 0); return
            ok, fail = 0, 0
            for item in items:
                if self._cancelled:
                    break
                name = item.get("Name", "Unknown")
                self.progress.emit(name)
                try:
                    self.api.delete(f"/Items/{item['Id']}", timeout=7)
                    logger.info("Maintenance: Deleted '%s'", name)
                    ok += 1
                except Exception as e:
                    logger.error("Maintenance: Failed '%s': %s", name, e)
                    fail += 1
            self.finished.emit(ok, fail)
        except Exception as e:
            logger.error("Maintenance crash: %s", e)
            self.finished.emit(0, -1)


class ContentLoaderThread(QThread):
    finished = pyqtSignal(list)
    progress = pyqtSignal(str)

    def __init__(self, api: EmbyAPISession, library_names: list[str]):
        super().__init__()
        self.api = api
        self.library_names = library_names

    def run(self):
        all_items: list[dict] = []
        try:
            views = self.api.get(f"/Users/{self.api.user_id}/Views", timeout=10).json().get("Items", [])
            view_map = {v["Name"]: v["Id"] for v in views}
            for lib in self.library_names:
                lid = view_map.get(lib)
                if not lid:
                    logger.warning("Library '%s' not found.", lib); continue
                self.progress.emit(f"Loading '{lib}'…")
                items = self.api.get(
                    f"/Users/{self.api.user_id}/Items",
                    params={
                        "ParentId": lid, "Recursive": "true",
                        "IncludeItemTypes": "Video,MusicVideo,Movie,Episode",
                        "Fields": "MediaSources,MediaStreams,UserData,Tags",
                        "Limit": "10000",
                    }, timeout=30,
                ).json().get("Items", [])
                logger.info("Library '%s': %d items", lib, len(items))
                all_items.extend(items)
        except Exception as e:
            logger.error("Content loader error: %s", e)
        self.finished.emit(all_items)


# ==============================================================================
# 4. CLICK SLIDER (jump-to-click instead of pageStep)
# ==============================================================================
class _ClickSlider(QSlider):
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderPosition(QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(),
                int(event.position().x()), self.width(),
            ))
        super().mousePressEvent(event)


# ==============================================================================
# 5. VIDEO CELL  (libmpv + Qt embedded via wid)
# ==============================================================================
class VideoCell(QWidget):
    """
    Layout: VBox [ video QFrame (stretch=1) ] [ controls QFrame (fixed) ]
    The video QFrame is a native window (WA_NativeWindow) whose winId() is
    handed to mpv via the 'wid' option. mpv renders directly into that HWND.
    Controls live below in a separate Qt surface — same VBox approach as 7.4
    (overlay-on-video still risks the same kind of HWND focus fight even with
    mpv, and we don't need over-video controls for this UX).

    Title overlay is a QLabel child of the *cell* (parented above the video
    frame in z-order). On Windows, a Qt child widget that is itself a native
    window can layer above the mpv-owned wid surface reliably.
    """

    request_next = pyqtSignal(object, bool)  # (cell, is_retry)
    request_prev = pyqtSignal(object)

    # Marshalling signals — mpv callbacks fire on its own thread; we re-emit
    # into Qt main via signals for safe widget access.
    # First arg is mpv generation counter — stale events from previously-
    # destroyed mpv instances arrive late on the Qt queue and would corrupt
    # the new instance's _played_anything tracking. Handlers drop events
    # whose gen != current.
    _sig_eof   = pyqtSignal(int, str)             # (gen, reason)
    _sig_time  = pyqtSignal(int, float, float)    # (gen, pos, dur)

    def __init__(self, controller: "WallController"):
        super().__init__()
        self.controller       = controller
        self.current_item: dict | None = None
        self.history: deque[dict] = deque(maxlen=50)
        self.looping          = False
        self.muted            = True
        self._dragging        = False
        self._retry_count     = 0
        self._force_transcode = False
        self.controls_visible = True
        self._mpv: "mpv.MPV | None" = None
        self._mpv_gen         = 0       # incremented per mpv instance; stale-event guard
        self._duration_s      = 0.0
        # Phase 2 stats (HYPERWALL_STATS=1):
        # _stats_current — counter snapshots from the live mpv instance; mpv
        # resets these to 0 on recreate, so we fold into _stats_total via
        # _flush_stats() before destroying.
        # _stats_total — lifetime accumulators across all mpv recreates this
        # cell has hosted. Read by the controller at shutdown.
        # _stats_info  — latest-value-wins informational props (hwdec, fps,
        # bitrate); overwritten freely.
        self._stats_current: dict[str, float]   = {}
        self._stats_total:   dict[str, float]   = {}
        self._stats_info:    dict[str, object]  = {}
        # Tracks whether playback ever advanced past frame 1 for the current
        # file. EOF-without-playback means the file failed to start (malformed
        # bitstream, broken HLS playlist, etc.) — treated as an error with
        # rate-limited retry, not a natural end-of-clip.
        self._played_anything = False
        # Defensive backstop: floor on time between consecutive next_video
        # calls per cell, in case any code path tries to fire requests faster
        # than playback can consume them. Prevents a runaway like 2026-05-03
        # when a malformed HEVC source emitted instant EOF in a tight loop.
        self._last_next_request_ts = 0.0
        # Emby PlaySessionId of the currently-loaded file. We tell Emby to
        # stop this session before starting a new one — without explicit
        # cleanup, Emby leaves the QSV transcoder running until its own
        # ~60s timeout, and skip-mashing exhausts the transcoder pool.
        self._emby_session_id: str | None = None
        self._emby_item_id:    str | None = None

        self.setStyleSheet("background: black;")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        vbox = QVBoxLayout(self)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(0)

        # Video host — must be a real native window for mpv to embed into
        self.video_frame = QFrame(self)
        self.video_frame.setStyleSheet("background: black;")
        self.video_frame.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.video_frame.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.video_frame.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors, True)
        self.video_frame.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        vbox.addWidget(self.video_frame, 1)

        self._build_controls()
        vbox.addWidget(self.controls_frame)

        # Auto-hide controls once on startup (then C is sole toggle)
        self._autohide_timer = QTimer(self)
        self._autohide_timer.setSingleShot(True)
        self._autohide_timer.timeout.connect(self._autohide_controls)
        self._autohide_timer.start(AUTOHIDE_MS)

        # Title overlay (QLabel layered above the cell, transparent to mouse)
        self._title_overlay = QLabel("", self)
        self._title_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_overlay.setWordWrap(False)
        self._title_overlay.setStyleSheet(
            "color: white; background: rgba(0,0,0,180);"
            " font-family: 'Segoe UI'; font-size: 13px; font-weight: 600;"
            " padding: 5px 14px; border-radius: 4px;"
        )
        self._title_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._title_overlay.hide()

        self._overlay_effect = QGraphicsOpacityEffect(self._title_overlay)
        self._title_overlay.setGraphicsEffect(self._overlay_effect)
        self._overlay_anim = QPropertyAnimation(self._overlay_effect, b"opacity", self)
        self._overlay_anim.setDuration(600)
        self._overlay_anim.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._overlay_anim.finished.connect(self._on_overlay_fade_done)
        self._overlay_show_timer = QTimer(self)
        self._overlay_show_timer.setSingleShot(True)
        self._overlay_show_timer.timeout.connect(self._fade_overlay_out)

        # Marshalled signal wiring
        self._sig_eof.connect(self._handle_eof, Qt.ConnectionType.QueuedConnection)
        self._sig_time.connect(self._handle_time, Qt.ConnectionType.QueuedConnection)

    # ── mpv lifecycle ─────────────────────────────────────────────────────────

    def _destroy_mpv(self):
        """Tear down the current mpv instance. Releases its claim on the wid
        HWND and any decoder/network state. Safe to call from Qt main thread."""
        if self._mpv is None:
            return
        if STATS_ENABLED:
            self._flush_stats()
        try:
            self._mpv.terminate()
        except Exception as e:
            logger.warning("mpv terminate raised: %s", e)
        self._mpv = None

    def _flush_stats(self):
        """Snapshot live counter properties off the current mpv instance, fold
        them into _stats_total, and clear _stats_current. Idempotent if mpv
        is already terminated. Called from _destroy_mpv and at shutdown."""
        if self._mpv is not None:
            for prop in STATS_COUNTER_PROPS:
                try:
                    v = self._mpv[prop]
                    if v is not None:
                        self._stats_current[prop] = float(v)
                except Exception:
                    pass
            for prop in STATS_INFO_PROPS:
                try:
                    v = self._mpv[prop]
                    if v is not None:
                        self._stats_info[prop] = v
                except Exception:
                    pass
        for k, v in self._stats_current.items():
            self._stats_total[k] = self._stats_total.get(k, 0.0) + v
        self._stats_current.clear()

    def _ensure_mpv(self):
        """Create the mpv instance lazily — winId() needs the widget realized
        (shown), and mpv claims the HWND immediately. v8 strategy: every play()
        destroys and recreates this instance to sidestep the python-mpv +
        libmpv multi-instance loadfile-reuse bug (mpv#16397, python-mpv#88)
        where subsequent loadfiles on a long-lived instance fail silently
        with EOF-before-first-frame after concurrent peers churn."""
        if self._mpv is not None:
            return
        wid = int(self.video_frame.winId())
        if wid == 0:
            logger.warning("video_frame.winId() == 0 — widget not realized yet.")
            return

        m = mpv.MPV(wid=wid, log_handler=self._mpv_log, **_apply_perf_env(MPV_OPTS))
        # Cells default to muted on construction; honor that before first play.
        try: m["mute"] = self.muted
        except Exception: pass
        # Loop state lives on the cell, not on mpv — apply it to each new
        # instance so the loop button stays "on" across destroy/recreate.
        if self.looping:
            try: m["loop-file"] = "inf"
            except Exception: pass

        # Bind a fresh generation to this instance. Closures capture it; the
        # Qt-side handlers compare against self._mpv_gen and discard events
        # whose gen != current. Without this, late-arriving observer events
        # from a previously-destroyed mpv corrupt the new instance's state
        # (specifically _played_anything, which gates EOF-vs-error routing).
        self._mpv_gen += 1
        gen = self._mpv_gen

        @m.event_callback("end-file")
        def _on_end_file(ev):
            try:
                reason = ev.event.get("reason", "eof")
            except Exception:
                reason = "eof"
            self._sig_eof.emit(gen, str(reason))

        @m.property_observer("time-pos")
        def _on_time(_name, value):
            if value is None:
                return
            if gen != self._mpv_gen:
                return  # stale event from previous instance
            if value > 0.05 and not self._played_anything:
                self._played_anything = True
            self._sig_time.emit(gen, float(value), float(self._duration_s or 0))

        @m.property_observer("duration")
        def _on_dur(_name, value):
            if gen != self._mpv_gen:
                return
            if value:
                self._duration_s = float(value)

        if STATS_ENABLED:
            # Counters are monotonic per mpv instance; observer fires only when
            # the value changes (i.e. a drop happens), so callback rate is
            # bounded by drop rate, not frame rate. Default-arg trick captures
            # gen + prop at definition time to avoid the late-binding closure
            # bug across the loop.
            for _prop in STATS_COUNTER_PROPS:
                @m.property_observer(_prop)
                def _on_counter(_name, value, _gen=gen, _prop=_prop):
                    if _gen != self._mpv_gen or value is None:
                        return
                    self._stats_current[_prop] = float(value)
            for _prop in STATS_INFO_PROPS:
                @m.property_observer(_prop)
                def _on_info(_name, value, _gen=gen, _prop=_prop):
                    if _gen != self._mpv_gen or value is None:
                        return
                    self._stats_info[_prop] = value

        self._mpv = m

    def _mpv_log(self, level, component, message):
        # Down-rank chatty mpv levels; surface real problems.
        text = message.strip()
        if level == "warn":
            for pat in _MPV_LOG_NOISE:
                if pat in text:
                    return  # known-benign demuxer noise; silently dropped
        msg = f"mpv[{component}] {text}"
        if level in ("fatal", "error"):
            logger.error(msg)
        elif level == "warn":
            logger.warning(msg)

    def showEvent(self, event):
        super().showEvent(event)
        # Realize the wid surface as early as possible so the controller's
        # staggered next_video() calls have something to target.
        self.video_frame.winId()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._title_overlay.isVisible():
            self._reposition_overlay()

    # ── Controls ──────────────────────────────────────────────────────────────

    def _build_controls(self):
        self.controls_frame = QFrame(self)
        self.controls_frame.setObjectName("controls")
        self.controls_frame.setFixedHeight(CONTROLS_HEIGHT)
        self.controls_frame.setStyleSheet(CTRL_STYLE)

        self._ctrl_effect = QGraphicsOpacityEffect(self.controls_frame)
        self.controls_frame.setGraphicsEffect(self._ctrl_effect)
        self._ctrl_anim = QPropertyAnimation(self._ctrl_effect, b"opacity", self)
        self._ctrl_anim.setDuration(150)
        self._ctrl_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._ctrl_anim.finished.connect(self._on_ctrl_fade_done)
        self._ctrl_effect.setOpacity(CONTROLS_OPACITY)

        outer = QVBoxLayout(self.controls_frame)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(1)

        self.seek_slider = _ClickSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setFixedHeight(10)
        self.seek_slider.sliderPressed.connect(self._seek_press)
        self.seek_slider.sliderReleased.connect(self._seek_release)
        outer.addWidget(self.seek_slider)

        row = QHBoxLayout()
        row.setSpacing(2); row.setContentsMargins(0, 0, 0, 0)

        def _btn(text: str, checkable: bool = False) -> QPushButton:
            b = QPushButton(text); b.setCheckable(checkable); return b

        self.btn_prev = _btn("⏮")
        self.btn_play = _btn("⏸")
        self.btn_next = _btn("⏭")
        self.btn_loop = _btn("🔁", checkable=True)
        self.btn_tag  = _btn("🗑", checkable=True)
        self.btn_fav  = _btn("⭐", checkable=True)
        self.btn_mute = _btn("🔇", checkable=True); self.btn_mute.setChecked(True)

        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100); self.vol_slider.setValue(0)
        self.vol_slider.setFixedWidth(45); self.vol_slider.setFixedHeight(10)

        self.lbl_time = QLabel("0:00 / 0:00")
        self.lbl_time.setFixedWidth(75)
        self.lbl_time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.lbl_title = QLabel("Initializing…")
        self.lbl_title.setStyleSheet(
            "color: white; font-family: 'Segoe UI'; font-size: 12px;"
            " font-weight: 700; background: transparent;"
        )

        for w in (self.btn_prev, self.btn_play, self.btn_next, self.btn_loop,
                  self.btn_tag, self.btn_fav, self.btn_mute):
            row.addWidget(w)
        row.addSpacing(2); row.addWidget(self.vol_slider)
        row.addSpacing(4); row.addWidget(self.lbl_time)
        row.addSpacing(2); row.addWidget(self.lbl_title, stretch=1)
        outer.addLayout(row)

        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_prev.clicked.connect(lambda: self.request_prev.emit(self))
        # User clicks bypass the throttle — they're not a runaway risk and
        # the recreate-per-play overhead (~50-100ms) naturally rate-limits.
        # Throttle stays on natural-EOF and retry paths only.
        self.btn_next.clicked.connect(lambda: self.request_next.emit(self, False))
        self.btn_loop.clicked.connect(self._toggle_loop)
        self.btn_tag.clicked.connect(self._toggle_tag)
        self.btn_fav.clicked.connect(self._toggle_fav)
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.vol_slider.valueChanged.connect(self._vol_changed)

    # ── Fade / overlay helpers ────────────────────────────────────────────────

    @staticmethod
    def _fmt_time(s: float) -> str:
        s = max(0, int(s))
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _fade_controls(self, visible: bool):
        self._ctrl_anim.stop()
        if visible:
            self.controls_frame.setVisible(True)
        self._ctrl_anim.setStartValue(self._ctrl_effect.opacity())
        self._ctrl_anim.setEndValue(CONTROLS_OPACITY if visible else 0.0)
        self._ctrl_anim.start()

    def _on_ctrl_fade_done(self):
        if self._ctrl_effect.opacity() < 0.01:
            self.controls_frame.setVisible(False)

    def _autohide_controls(self):
        self.controls_visible = False
        self.controller.controls_visible = False
        self._fade_controls(False)

    def set_controls_visible(self, visible: bool):
        self.controls_visible = visible
        self._autohide_timer.stop()
        self._fade_controls(visible)

    def _show_title_overlay(self, title: str):
        self._overlay_show_timer.stop()
        self._overlay_anim.stop()
        self._title_overlay.setText(title)
        self._overlay_effect.setOpacity(1.0)
        self._title_overlay.adjustSize()
        self._reposition_overlay()
        self._title_overlay.show()
        self._title_overlay.raise_()
        self._overlay_show_timer.start(OVERLAY_SHOW_MS)

    def _reposition_overlay(self):
        vw, ovl = self.video_frame, self._title_overlay
        ovl.adjustSize()
        w = min(ovl.sizeHint().width(), max(vw.width() - 24, 0))
        h = ovl.sizeHint().height()
        x = vw.x() + (vw.width() - w) // 2
        y = vw.y() + vw.height() - h - 20
        ovl.setFixedWidth(w); ovl.move(x, y)

    def _fade_overlay_out(self):
        self._overlay_anim.setStartValue(1.0)
        self._overlay_anim.setEndValue(0.0)
        self._overlay_anim.start()

    def _on_overlay_fade_done(self):
        if self._overlay_effect.opacity() < 0.01:
            self._title_overlay.hide()

    # ── Playback ──────────────────────────────────────────────────────────────

    def play(self, item: dict, url: str):
        # NB: only reset retry state when starting a NEW item. Retries hand
        # the same item back to play(); resetting _retry_count there would
        # make the exponential backoff never escalate, producing an
        # infinite 2s-spaced retry loop on any unplayable file.
        if self.current_item is not item:
            self._retry_count     = 0
            self._force_transcode = False
        self.current_item     = item
        self._duration_s      = 0.0
        self._played_anything = False

        title = item.get("Name", "Unknown")
        self.lbl_title.setText(title)

        raw = item.get("Tags", [])
        tag_names = ([t.get("Name", "") for t in raw]
                     if raw and isinstance(raw[0], dict) else raw)
        self.btn_tag.setChecked("ToDelete" in tag_names)
        self.btn_fav.setChecked(item.get("UserData", {}).get("IsFavorite", False))

        # Tear down + recreate mpv per play. Sidesteps the python-mpv +
        # libmpv multi-instance loadfile-reuse bug — every transition becomes
        # equivalent to fresh wall startup which we know works at 100%.
        # Cost: ~50-100ms (mpv init + HWND reattach + first frame). Visible
        # as a brief black flash between videos. Acceptable trade vs. the
        # filter-change-breaks-the-wall failure mode.
        self._destroy_mpv()
        self._ensure_mpv()
        if self._mpv is None:
            logger.error("mpv not initialized — cannot play.")
            return
        try:
            self._mpv["mute"] = self.muted
            self._mpv.command("loadfile", url)
            self.btn_play.setText("⏸")
        except Exception as e:
            logger.error("mpv loadfile failed: %s", e)
            self._sig_eof.emit(self._mpv_gen, "error")
            return
        self._show_title_overlay(title)

    def release(self):
        self._destroy_mpv()

    # ── mpv-driven slots (run on Qt main thread) ──────────────────────────────

    def _handle_eof(self, gen: int, reason: str):
        # Drop stale events from previously-destroyed mpv instances.
        if gen != self._mpv_gen:
            return
        if reason == "error":
            self._on_error()
            return
        if reason == "eof":
            # EOF without ever advancing playback = file failed to start
            # (malformed bitstream, broken HLS, etc.). Route through the error
            # path so the rate-limited backoff applies — otherwise next_video
            # fires instantly and a bad cluster of files becomes a runaway.
            if not self._played_anything:
                logger.warning("EOF before first frame — treating as error.")
                self._on_error()
                return
            if self.looping and self._mpv is not None:
                try:
                    self._mpv.seek(0, "absolute")
                    self._mpv["pause"] = False
                except Exception:
                    pass
            else:
                self._request_next_throttled(False)
        # 'stop'/'quit'/'redirect' → ignore

    def _request_next_throttled(self, is_retry: bool):
        """Defensive backstop on the request_next signal. Drops calls that
        arrive within MIN_NEXT_INTERVAL_S of the previous one — ensures no
        future bug can cause a tight next-video loop. Retry path bypasses the
        throttle since _on_error already enforces exponential backoff."""
        import time as _time
        MIN_NEXT_INTERVAL_S = 0.75
        now = _time.monotonic()
        if not is_retry and (now - self._last_next_request_ts) < MIN_NEXT_INTERVAL_S:
            logger.warning("next_video throttled (last fire %.2fs ago)",
                           now - self._last_next_request_ts)
            return
        self._last_next_request_ts = now
        self.request_next.emit(self, is_retry)

    def _on_error(self):
        self._retry_count += 1
        logger.warning("Playback error (attempt %d/%d)", self._retry_count, MAX_RETRIES)
        if self._retry_count <= MAX_RETRIES:
            if self._retry_count >= 2 and not self._force_transcode:
                self._force_transcode = True
                logger.info("Escalating to server transcode after repeated failures.")
            QTimer.singleShot((2 ** self._retry_count) * 1000,
                              lambda: self._request_next_throttled(True))
        else:
            logger.error("Max retries reached — skipping.")
            self._force_transcode = False
            self._request_next_throttled(False)

    def _handle_time(self, gen: int, pos: float, dur: float):
        if gen != self._mpv_gen:
            return  # stale event from previous mpv instance
        # Skip slider/label paint when controls are hidden.
        if not self.controls_visible:
            return
        if not self._dragging and dur > 0:
            self.seek_slider.setValue(int(pos / dur * 1000))
        self.lbl_time.setText(f"{self._fmt_time(pos)} / {self._fmt_time(dur)}")

    # ── Control wiring ────────────────────────────────────────────────────────

    def _seek_press(self):
        self._dragging = True
        self._autohide_timer.stop()
        if self._mpv is not None:
            try: self._mpv["pause"] = True
            except Exception: pass

    def _seek_release(self):
        if self._mpv is not None and self._duration_s > 0:
            try:
                # Cap to 90% so right-edge clicks land well before EOF.
                frac = min(self.seek_slider.value() / 1000.0, 0.90)
                target = frac * self._duration_s
                # Keyframe seek (no "exact") -- frame-accurate seek forces
                # decode from nearest keyframe forward and was the source of
                # the seek-causes-stutter regression. Sub-second seek
                # precision lost; visually invisible for clip-rotation use.
                self._mpv.seek(target, "absolute")
                self._mpv["pause"] = False
                self.btn_play.setText("⏸")
            except Exception as e:
                logger.warning("seek failed: %s", e)
        self._dragging = False

    def _toggle_play(self):
        if self._mpv is None: return
        try:
            new_pause = not bool(self._mpv["pause"])
            self._mpv["pause"] = new_pause
            self.btn_play.setText("▶" if new_pause else "⏸")
        except Exception:
            pass

    def _toggle_loop(self):
        self.looping = self.btn_loop.isChecked()
        # Use mpv's native loop-file property — keep_open=no closes the file
        # on EOF, so a Qt-side seek(0) hits an idle player. Letting mpv
        # handle the loop internally keeps the file open and avoids a
        # full destroy/recreate cycle on each repeat.
        if self._mpv is not None:
            try:
                self._mpv["loop-file"] = "inf" if self.looping else "no"
            except Exception:
                pass

    def _toggle_mute(self):
        muted = self.btn_mute.isChecked()
        self.muted = muted
        if self._mpv is not None:
            try: self._mpv["mute"] = muted
            except Exception: pass
        self.btn_mute.setText("🔇" if muted else "🔊")
        if not muted and self.vol_slider.value() == 0:
            self.vol_slider.setValue(70)

    def _vol_changed(self, val: int):
        if self._mpv is not None:
            try: self._mpv["volume"] = float(val)
            except Exception: pass
        if val > 0 and self.muted:
            self.muted = False
            if self._mpv is not None:
                try: self._mpv["mute"] = False
                except Exception: pass
            self.btn_mute.setChecked(False); self.btn_mute.setText("🔊")
        elif val == 0 and not self.muted:
            self.muted = True
            if self._mpv is not None:
                try: self._mpv["mute"] = True
                except Exception: pass
            self.btn_mute.setChecked(True); self.btn_mute.setText("🔇")

    def _toggle_tag(self):
        if not self.current_item: return
        raw = self.current_item.setdefault("Tags", [])
        tags = ([t.get("Name", "") for t in raw]
                if raw and isinstance(raw[0], dict) else list(raw))
        if "ToDelete" in tags: tags.remove("ToDelete")
        else:                  tags.append("ToDelete")
        self.current_item["Tags"] = tags
        self.btn_tag.setChecked("ToDelete" in tags)
        self.controller.update_tags(self.current_item)

    def _toggle_fav(self):
        if not self.current_item: return
        new = self.btn_fav.isChecked()
        self.current_item.setdefault("UserData", {})["IsFavorite"] = new
        self.controller.update_favorite(self.current_item["Id"], new)


# ==============================================================================
# 6. WALL CONTROLLER
# ==============================================================================
class WallController:
    """Owns cells, windows, routing, global shortcuts, and API workers."""

    def __init__(self, settings: dict, api: EmbyAPISession):
        self.settings   = settings
        self.api        = api
        self.cells:    list[VideoCell]   = []
        self.windows:  list[QMainWindow] = []
        self.all_items: list[dict] = []
        self.filtered:  list[dict] = []
        self.playlist:  deque[dict] = deque()
        self.controls_visible = True

        self._build_displays()
        self._start_async_load()

    # ── Display setup ─────────────────────────────────────────────────────────

    def _build_displays(self):
        rows, cols = self.settings["grid"]
        for screen in self.settings["screens"]:
            win = QMainWindow()
            win.setWindowTitle(f"HyperWall — {screen.name()}")
            win.setStyleSheet("background: black;")

            cw = QWidget(); win.setCentralWidget(cw)
            grid = QGridLayout(cw); grid.setContentsMargins(0, 0, 0, 0); grid.setSpacing(0)

            for r in range(rows):
                for c in range(cols):
                    cell = VideoCell(self)
                    cell.request_next.connect(self.next_video)
                    cell.request_prev.connect(self.prev_video)
                    grid.addWidget(cell, r, c)
                    self.cells.append(cell)

            for key, fn in (
                ("C",      self._global_toggle_controls),
                ("Space",  self._global_toggle_pause),
                ("F",      lambda: self._set_filter("favorites")),
                ("A",      lambda: self._set_filter("all")),
                ("S",      self._toggle_stats_overlay),
                ("R",      self._open_remix_dialog),
                ("Escape", self._shutdown),
            ):
                QShortcut(QKeySequence(key), win).activated.connect(fn)

            win.setGeometry(screen.geometry())
            win.showFullScreen()
            self.windows.append(win)
            logger.info("Display active: %s", screen.name())

    # ── Async content load ────────────────────────────────────────────────────

    def _start_async_load(self):
        self.loader = ContentLoaderThread(self.api, self.settings["libraries"])
        self.loader.finished.connect(self._on_items_loaded)
        self.loader.start()

    def _on_items_loaded(self, items: list[dict]):
        self.all_items = items
        self.filtered  = items[:]
        logger.info("Metadata Index: %d items loaded.", len(items))
        if not items:
            logger.warning("No items returned — check config.ini libraries.")
            for cell in self.cells:
                lbl = QLabel("No items found—check config.ini libraries", cell)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl.setStyleSheet(
                    "color: #666; font-size: 13px; font-family: 'Segoe UI';"
                    " background: transparent;"
                )
                lbl.resize(cell.video_frame.size()); lbl.show()
            return
        for i, cell in enumerate(self.cells):
            QTimer.singleShot(i * STREAM_START_STAGGER_MS,
                              lambda c=cell: self.next_video(c, False))

    # ── Stream URL — always-REMUX, with one transcode escape on retry ─────────

    def _build_url(self, item: dict, force_transcode: bool = False) -> tuple[str, str]:
        """Returns (url, session_id). Hybrid routing per Phase 2 audit:

          DIRECT  — source ≤1080p AND ≤30fps. Wall PC decodes natively on
                    NVDEC, no server load beyond TCP file serve.
          TRANSCODE — source >1080p OR >30fps. Routed through Emby's QSV
                    transcoder to 1080p@30 H.264 + AAC. Caps wall-side
                    render cost regardless of source attributes.
          TRANSCODE/retry — set when a cell escalates after 2 failures.
                    Same URL shape; defensive escape for any source mpv
                    can't handle natively.
        """
        iid  = item["Id"]
        key  = self.api.access_token
        base = self.api.server_url
        sid  = uuid.uuid4().hex

        auto_transcode = _needs_transcode(item)
        if force_transcode or auto_transcode:
            # MaxFramerate=30 caps source frame rate before encode (Emby
            # passes through if source ≤ cap). VideoBitrate=12M is plenty
            # for 1080p H.264 at wall viewing distance.
            url = (f"{base}/Videos/{iid}/master.m3u8?api_key={key}"
                   f"&VideoCodec=h264&AudioCodec=aac&MaxAudioChannels=2"
                   f"&MaxHeight=1080&MaxWidth=1920"
                   f"&MaxFramerate=30&VideoBitrate=12000000"
                   f"&PlaySessionId={sid}")
            tag = "TRANSCODE/retry" if force_transcode else "TRANSCODE/auto"
            logger.info("[%s] %s", tag, item.get("Name"))
        else:
            url = f"{base}/Videos/{iid}/stream?api_key={key}&static=true"
            logger.info("[DIRECT] %s", item.get("Name"))
        return url, sid

    def stop_emby_session(self, item_id: str | None, session_id: str | None):
        """Fire-and-forget: notify Emby that a play session ended so it kills
        the associated transcoder. Skip-mashing without this leaks transcoder
        slots until the QSV pool exhausts and new requests hang."""
        if not item_id or not session_id:
            return
        def _worker():
            try:
                r = self.api.post("/Sessions/Playing/Stopped",
                                  json={"ItemId": item_id,
                                        "PlaySessionId": session_id,
                                        "PositionTicks": 0},
                                  timeout=5)
                logger.info("Session stop %s -> HTTP %d", session_id[:8], r.status_code)
            except Exception as e:
                logger.warning("Stop-session %s failed: %s", session_id[:8], e)
        threading.Thread(target=_worker, daemon=True).start()

    def _hand_off(self, cell: VideoCell, item: dict, force_transcode: bool = False):
        """Stop the cell's previous Emby session, build a new URL, hand to
        cell. Centralizes the stop-then-start pattern so retry/next/prev all
        get the same treatment."""
        self.stop_emby_session(cell._emby_item_id, cell._emby_session_id)
        url, sid = self._build_url(item, force_transcode)
        cell._emby_session_id = sid
        cell._emby_item_id    = item["Id"]
        cell.play(item, url)

    def next_video(self, cell: VideoCell, is_retry: bool = False):
        if not self.filtered: return
        if is_retry and cell.current_item:
            self._hand_off(cell, cell.current_item, cell._force_transcode)
            return
        if cell.current_item:
            cell.history.append(cell.current_item)
        if not self.playlist:
            shuffled = self.filtered[:]; random.shuffle(shuffled)
            self.playlist = deque(shuffled)
        item = self.playlist.pop()
        self._hand_off(cell, item)

    def prev_video(self, cell: VideoCell):
        if cell.history:
            item = cell.history.pop()
            self._hand_off(cell, item)

    # ── Global shortcuts ──────────────────────────────────────────────────────

    def _global_toggle_controls(self):
        self.controls_visible = not self.controls_visible
        for c in self.cells:
            c.set_controls_visible(self.controls_visible)
        logger.info("Controls: %s", "VISIBLE" if self.controls_visible else "HIDDEN")

    def _open_remix_dialog(self):
        """Hotkey 'R' — opens the wall-remix dialog (folder picker → ssh greg
        → trio_remix_mv.py).  Companion module hyperwall_remix.py owns the UI
        and the QThread that streams logs; safe to call repeatedly."""
        if _remix_walls is None:
            logger.warning("Remix unavailable: hyperwall_remix module missing.")
            return
        parent = self.windows[0] if self.windows else None
        try:
            _remix_walls(parent)
        except Exception:
            logger.exception("Remix dialog failed to launch")

    def _global_toggle_pause(self):
        any_playing = False
        for c in self.cells:
            if c._mpv is not None:
                try:
                    if not bool(c._mpv["pause"]):
                        any_playing = True; break
                except Exception: pass
        for c in self.cells:
            if c._mpv is None: continue
            try:
                c._mpv["pause"] = any_playing
                c.btn_play.setText("▶" if any_playing else "⏸")
            except Exception: pass

    def _set_filter(self, mode: str):
        if mode == "favorites":
            subset = [i for i in self.all_items if i.get("UserData", {}).get("IsFavorite")]
            if not subset:
                logger.warning("Filter: No favorites found."); return
            self.filtered = subset
        else:
            self.filtered = self.all_items[:]
        self.playlist.clear()
        logger.info("Filter: %s (%d items)", mode.upper(), len(self.filtered))
        # Stagger same as initial startup — firing 8-12 cells simultaneously
        # makes the network burst look like a thundering herd, even with the
        # 2-tier routing reducing server load.
        for i, c in enumerate(self.cells):
            QTimer.singleShot(i * STREAM_START_STAGGER_MS,
                              lambda cell=c: self.next_video(cell, False))

    # ── API mutations (background threads) ────────────────────────────────────

    def update_tags(self, item: dict):
        iid  = item["Id"]
        name = item.get("Name", "Unknown")
        raw  = item.get("Tags", [])
        tags = ([t.get("Name", "") for t in raw]
                if raw and isinstance(raw[0], dict) else list(raw))

        def _worker():
            try:
                data = self.api.get(f"/Users/{self.api.user_id}/Items/{iid}", timeout=7).json()
                data["Tags"] = tags
                # Strip read-only / server-managed fields — POST/PUT will reject
                # the item otherwise. List ported verbatim from 7.4.
                for k in ("ServerId", "Etag", "DateCreated", "CanDelete", "CanDownload",
                          "UserData", "Chapters", "ImageTags", "BackdropImageTags",
                          "TagItems", "ExternalUrls", "PlayAccess"):
                    data.pop(k, None)
                self.api.post(f"/Items/{iid}", json=data, timeout=7)
                logger.info("API: Tags updated for '%s'", name)
            except Exception as e:
                logger.error("API: Tag error for '%s': %s", name, e)

        threading.Thread(target=_worker, daemon=True).start()

    def update_favorite(self, item_id: str, state: bool):
        def _worker():
            try:
                path = f"/Users/{self.api.user_id}/FavoriteItems/{item_id}"
                (self.api.post if state else self.api.delete)(path, timeout=7)
                logger.info("API: Favorite toggled for %s → %s", item_id, state)
            except Exception as e:
                logger.error("API: Favorite error: %s", e)
        threading.Thread(target=_worker, daemon=True).start()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _shutdown(self):
        logger.info("Shutdown requested.")
        QApplication.instance().quit()

    def _cleanup(self):
        # Stop Emby sessions first so transcoders die immediately rather than
        # waiting out the server-side timeout. Best-effort, fire-and-forget.
        for c in self.cells:
            self.stop_emby_session(c._emby_item_id, c._emby_session_id)
        if STATS_ENABLED:
            # Snapshot any still-live counters before terminate clears them.
            for c in self.cells:
                try: c._flush_stats()
                except Exception as e:
                    logger.warning("stats flush failed: %s", e)
        for c in self.cells:
            try: c.release()
            except Exception: pass
        if STATS_ENABLED:
            self._dump_stats_json()
        self.api.close()
        logger.info("Cleanup complete.")

    # ── Stats overlay + dump (HYPERWALL_STATS=1) ──────────────────────────────

    def _toggle_stats_overlay(self):
        """Toggle mpv's built-in stats.lua overlay on cell 0. Available on `S`.
        Page 2 (decoder/render timing) is more useful than page 1 for our
        purpose; cycle: toggle on -> page 2."""
        if not self.cells:
            return
        cell = self.cells[0]
        if cell._mpv is None:
            logger.info("Stats: cell 0 has no live mpv yet.")
            return
        try:
            cell._mpv.command("script-binding", "stats/display-stats-toggle")
            cell._mpv.command("script-binding", "stats/display-page-2")
            logger.info("Stats overlay toggled on cell 0 (page 2).")
        except Exception as e:
            logger.warning("Stats overlay toggle failed (stats.lua not loaded?): %s", e)

    def _dump_stats_json(self):
        """Write per-cell stats to a timestamped JSON next to hyperwall.log,
        and emit a compact one-line-per-cell summary into the regular log."""
        import json, time
        cells_payload = []
        for i, c in enumerate(self.cells):
            cells_payload.append({
                "cell": i,
                "totals": dict(c._stats_total),
                "info":   {k: v for k, v in c._stats_info.items()},
                "last_item": (c.current_item or {}).get("Name"),
            })
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "n_cells": len(self.cells),
            "mpv_opts_effective": _apply_perf_env(MPV_OPTS),
            "env": {k: os.environ.get(k) for k in (
                "HYPERWALL_STATS", "HYPERWALL_HDR_HINT", "HYPERWALL_HWDEC",
                "HYPERWALL_GPU_API", "HYPERWALL_PROFILE", "HYPERWALL_VIDEO_SYNC",
            ) if os.environ.get(k) is not None},
            "cells": cells_payload,
        }
        out = os.path.join(SCRIPT_DIR, f"hyperwall_stats_{int(time.time())}.json")
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            logger.info("STATS dump: %s", out)
        except Exception as e:
            logger.warning("STATS dump failed: %s", e)
            return
        for s in cells_payload:
            t = s["totals"]
            i = s["info"]
            logger.info(
                "STATS cell %d  drop=%g  mistimed=%g  vo-delayed=%g  dec-drop=%g"
                "  hwdec=%s  fps=%s  bitrate=%s",
                s["cell"],
                t.get("frame-drop-count", 0),
                t.get("mistimed-frame-count", 0),
                t.get("vo-delayed-frame-count", 0),
                t.get("decoder-frame-drop-count", 0),
                i.get("hwdec-current"),
                i.get("estimated-vf-fps") or i.get("container-fps"),
                i.get("video-bitrate"),
            )


# ==============================================================================
# 7. MOUSE IDLE HIDER
# ==============================================================================
class MouseIdleHider(QObject):
    def __init__(self):
        super().__init__()
        self._hidden = False
        self._timer = QTimer(); self._timer.setSingleShot(True)
        self._timer.setInterval(MOUSE_IDLE_MS)
        self._timer.timeout.connect(self._hide)
        QApplication.instance().installEventFilter(self)
        self._timer.start()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseMove:
            if self._hidden:
                QApplication.restoreOverrideCursor()
                self._hidden = False
            self._timer.start()
        return False

    def _hide(self):
        if not self._hidden:
            QApplication.setOverrideCursor(Qt.CursorShape.BlankCursor)
            self._hidden = True


# ==============================================================================
# 8. SETUP WIZARD
# ==============================================================================
class SetupWizard(QDialog):
    def __init__(self, config: configparser.ConfigParser, screens, libraries: list[str]):
        super().__init__()
        self.setWindowTitle("HyperWall 8.0")
        self.resize(720, 540)
        self.setStyleSheet("""
            QDialog { background: #0e0e0e; color: #eee; font-family: 'Segoe UI'; }
            QGroupBox {
                border: 1px solid #2a2a2a; border-radius: 4px; margin-top: 8px;
                font-weight: bold; font-size: 11px; color: #3b8edb; background: #141414;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
            QListWidget { background: #181818; border: 1px solid #2a2a2a; color: #ccc; outline: none; }
            QListWidget::item:selected { background: #1e4f78; color: white; }
            QSpinBox { background: #181818; color: white; border: 1px solid #333; padding: 4px; min-width: 50px; }
            QPushButton {
                background: #1e4f78; color: white; border: none; padding: 10px 24px;
                font-weight: bold; border-radius: 4px; font-size: 13px;
                min-width: 0; min-height: 0; max-width: 9999px; max-height: 9999px;
            }
            QPushButton:hover { background: #3b8edb; }
            QLabel { color: #888; font-size: 11px; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20); layout.setSpacing(14)

        title = QLabel("HYPERWALL  8.0")
        title.setStyleSheet("font-size: 24px; font-weight: 900; color: white; letter-spacing: 3px;")
        layout.addWidget(title)

        panels = QHBoxLayout(); panels.setSpacing(14)

        grp_disp = QGroupBox("DISPLAYS")
        l_disp = QVBoxLayout(grp_disp)
        self.list_disp = QListWidget()
        self.list_disp.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self._screen_map: dict[str, object] = {}
        last_screens = config.get("Settings", "last_screens", fallback="").split(",")
        for s in screens:
            label = f"{s.name()}  [{s.geometry().width()}×{s.geometry().height()}]"
            item = QListWidgetItem(label); self.list_disp.addItem(item)
            self._screen_map[label] = s
            if s.name() in last_screens:
                item.setSelected(True)
        l_disp.addWidget(self.list_disp); panels.addWidget(grp_disp)

        grp_lib = QGroupBox("SOURCES")
        l_lib = QVBoxLayout(grp_lib)
        self.list_lib = QListWidget()
        self.list_lib.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        last_libs = config.get("Settings", "last_libraries", fallback="").split(",")
        for lib in libraries:
            item = QListWidgetItem(lib); self.list_lib.addItem(item)
            if lib in last_libs:
                item.setSelected(True)
        l_lib.addWidget(self.list_lib); panels.addWidget(grp_lib)

        layout.addLayout(panels)

        grp_grid = QGroupBox("LAYOUT")
        l_grid = QHBoxLayout(grp_grid)
        self.rows = QSpinBox(); self.rows.setRange(1, 6)
        self.rows.setValue(int(config.get("Settings", "last_grid_rows", fallback="2")))
        self.cols = QSpinBox(); self.cols.setRange(1, 6)
        self.cols.setValue(int(config.get("Settings", "last_grid_cols", fallback="2")))
        l_grid.addWidget(QLabel("ROWS")); l_grid.addWidget(self.rows)
        l_grid.addSpacing(20)
        l_grid.addWidget(QLabel("COLS")); l_grid.addWidget(self.cols)
        l_grid.addStretch()
        btn = QPushButton("▶   INITIALIZE SYSTEM"); btn.clicked.connect(self.accept)
        l_grid.addWidget(btn)
        layout.addWidget(grp_grid)

    def get_settings(self) -> dict:
        return {
            "screens":   [self._screen_map[i.text()] for i in self.list_disp.selectedItems()],
            "libraries": [i.text() for i in self.list_lib.selectedItems()],
            "grid":      (self.rows.value(), self.cols.value()),
        }


# ==============================================================================
# 9. ENTRY POINT
# ==============================================================================
def main():
    # 1. NVIDIA isolation: re-exec into bundled launcher if present.
    maybe_relaunch_in_isolation()

    # 2. libmpv presence check (python-mpv ImportError is usually missing DLL).
    if mpv is None:
        msg = (f"python-mpv failed to load: {_MPV_IMPORT_ERR}\n\n"
               f"Install:\n  pip install python-mpv\n\n"
               f"And place mpv-2.dll next to this script:\n  {SCRIPT_DIR}\n\n"
               f"Download: https://mpv.io/installation/  (use shobon-mpv builds)")
        # Try to show a dialog if Qt is up; else stderr.
        try:
            app = QApplication.instance() or QApplication(sys.argv)
            QMessageBox.critical(None, "HyperWall — libmpv missing", msg)
        except Exception:
            print(msg, file=sys.stderr)
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 3. Verify/apply NVIDIA profile (no-op if launcher basename mismatch).
    ensure_nvidia_profile()

    _mouse_hider = MouseIdleHider()  # noqa: F841 — kept alive by ref

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
    _eff = _apply_perf_env(MPV_OPTS)
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
