"""
Hyperwall — all tunable constants in one place.

MPV hardware tuning targets: NVIDIA Blackwell (nvdec/d3d11) + 240 Hz UltraGear.
Values chosen for low-latency multi-cell playback with HDR hinting.
"""

from __future__ import annotations

import os
import sys

# ── Paths ────────────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
    LAUNCH_BASENAME = os.path.basename(sys.executable).lower()
else:
    SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    LAUNCH_BASENAME = os.path.basename(sys.executable).lower()

CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.ini")
LOG_FILE = os.path.join(SCRIPT_DIR, "hyperwall.log")
LAUNCHER_EXE = os.path.join(SCRIPT_DIR, "hyperwall.exe")
NIP_FILE = os.path.join(SCRIPT_DIR, "hyperwall.nip")
NPI_DIR = os.path.join(SCRIPT_DIR, "tools")
NPI_EXE = os.path.join(NPI_DIR, "nvidiaProfileInspector.exe")
NV_SENTINEL = os.path.join(SCRIPT_DIR, ".hyperwall_nvprofile.sentinel")

# ── UI scale ─────────────────────────────────────────────────────────────────
# Controls are authored at 1.0 = 1080p-ish density. On 4K panels set
# HYPERWALL_UI_SCALE=1.5 (or 2.0) for legible controls.


def _ui_scale() -> float:
    try:
        return max(0.5, min(3.0, float(os.environ.get("HYPERWALL_UI_SCALE", "1.0"))))
    except ValueError:
        return 1.0


UI_SCALE = _ui_scale()

# ── Timing ───────────────────────────────────────────────────────────────────
STREAM_START_STAGGER_MS = 300   # ms between cell starts
MAX_RETRIES = 3                 # then skip the dead stream
CONTROLS_HEIGHT = int(44 * UI_SCALE)  # px
CONTROLS_OPACITY = 0.82
AUTOHIDE_MS = 5_000             # one-shot startup auto-hide
OVERLAY_SHOW_MS = 3_000         # title overlay before fade
MOUSE_IDLE_MS = 3_000           # cursor auto-hide

# ── Reliability / self-healing (Epic 2) ──────────────────────────────────────
# Stall watchdog: if time-pos hasn't advanced for STALL_TIMEOUT_S while a cell
# is actively playing (not paused/seeking), treat it as a silent freeze and run
# the normal error/escalation chain. WATCHDOG_INTERVAL_MS is the poll cadence.


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


STALL_TIMEOUT_S = _int_env("HYPERWALL_STALL_TIMEOUT_S", 20, 3, 600)
WATCHDOG_INTERVAL_MS = _int_env("HYPERWALL_WATCHDOG_MS", 5_000, 1_000, 60_000)

# Crash-loop guard: if a cell records CRASH_LOOP_THRESHOLD failures within
# CRASH_LOOP_WINDOW_S, park it on a "media unavailable" card instead of
# hammering Emby. It re-attempts after CRASH_LOOP_COOLDOWN_S.
CRASH_LOOP_THRESHOLD = _int_env("HYPERWALL_CRASHLOOP_THRESHOLD", 5, 2, 100)
CRASH_LOOP_WINDOW_S = _int_env("HYPERWALL_CRASHLOOP_WINDOW_S", 60, 10, 3_600)
CRASH_LOOP_COOLDOWN_S = _int_env("HYPERWALL_CRASHLOOP_COOLDOWN_S", 120, 10, 7_200)

# Memory-aware demuxer cache budget. Each cell wants PER_CELL demuxer bytes, but
# the grid total is capped at CACHE_BUDGET_MB so large grids don't blow up RAM.
DEMUXER_PER_CELL_MB = _int_env("HYPERWALL_DEMUXER_PER_CELL_MB", 512, 32, 2_048)
CACHE_BUDGET_MB = _int_env("HYPERWALL_CACHE_BUDGET_MB", 3_072, 128, 65_536)

# ── MPV Options ──────────────────────────────────────────────────────────────
MPV_OPTS: dict[str, object] = dict(
    vo="gpu-next",
    gpu_api="d3d11",
    hwdec="nvdec-copy",
    profile="fast",
    video_sync="audio",
    video_sync_max_video_change=5,
    interpolation="no",
    target_colorspace_hint="yes",
    cache="yes",
    cache_secs=30,
    demuxer_max_bytes="512MiB",
    demuxer_readahead_secs=30,
    network_timeout=15,
    stream_lavf_o="reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
    keep_open="always",
    force_window="no",
    idle="yes",
    osd_level=0,
    input_default_bindings=False,
    input_vo_keyboard=False,
    ytdl=False,
    ao="wasapi,null",
    audio_buffer=2.0,
    msg_level="all=warn,cplayer=info,ao=error,ao/wasapi=fatal",
)

STATS_ENABLED = os.environ.get("HYPERWALL_STATS") == "1"

STATS_COUNTER_PROPS = (
    "frame-drop-count",
    "mistimed-frame-count",
    "vo-delayed-frame-count",
    "decoder-frame-drop-count",
)

STATS_INFO_PROPS = (
    "hwdec-current",
    "video-bitrate",
    "container-fps",
    "estimated-vf-fps",
)

# ── MPV log noise to suppress ────────────────────────────────────────────────
MPV_LOG_NOISE = (
    "UDTA parsing failed retrying raw",
    "Detected creation time before 1970",
    "Unknown cover type",
    "stream 0, timescale not set",
    "client removed during hook handling",
    "Immediate exit requested",
    "Leaking 1 nested connections",
)


def apply_env_overrides(opts: dict) -> dict:
    """Apply environment variable overrides to MPV_OPTS copy."""
    out = dict(opts)
    for env_var, key in (
        ("HYPERWALL_VO", "vo"),
        ("HYPERWALL_HWDEC", "hwdec"),
        ("HYPERWALL_GPU_API", "gpu_api"),
        ("HYPERWALL_PROFILE", "profile"),
        ("HYPERWALL_VIDEO_SYNC", "video_sync"),
    ):
        if v := os.environ.get(env_var):
            out[key] = v
    if (v := os.environ.get("HYPERWALL_HDR_HINT")) is not None:
        out["target_colorspace_hint"] = "yes" if v == "1" else "no"
    if (v := os.environ.get("HYPERWALL_AUDIO_BUFFER")) is not None:
        try:
            out["audio_buffer"] = float(v)
        except ValueError:
            pass
    return out


def apply_cache_budget(opts: dict, n_cells: int) -> dict:
    """Return a copy of opts with demuxer_max_bytes scaled to the cell count.

    Keeps the aggregate grid demuxer buffer within CACHE_BUDGET_MB so large
    grids (e.g. 6x6) don't exhaust RAM. Pure w.r.t. the reliability helper.
    """
    from .reliability import scale_demuxer_mb

    out = dict(opts)
    mb = scale_demuxer_mb(
        n_cells,
        per_cell_mb=DEMUXER_PER_CELL_MB,
        total_budget_mb=CACHE_BUDGET_MB,
    )
    out["demuxer_max_bytes"] = f"{mb}MiB"
    return out
