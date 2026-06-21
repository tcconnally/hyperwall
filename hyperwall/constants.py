"""
Hyperwall v9 — all tunable constants in one place.

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
LAUNCHER_EXE = os.path.join(SCRIPT_DIR, "hyperwall_v8.exe")
NIP_FILE = os.path.join(SCRIPT_DIR, "hyperwall.nip")
NPI_DIR = os.path.join(SCRIPT_DIR, "tools")
NPI_EXE = os.path.join(NPI_DIR, "nvidiaProfileInspector.exe")
NV_SENTINEL = os.path.join(SCRIPT_DIR, ".hyperwall_v8_nvprofile.sentinel")

# ── Timing ───────────────────────────────────────────────────────────────────
STREAM_START_STAGGER_MS = 300   # ms between cell starts
MAX_RETRIES = 3                 # then skip the dead stream
CONTROLS_HEIGHT = 44            # px
CONTROLS_OPACITY = 0.82
AUTOHIDE_MS = 5_000             # one-shot startup auto-hide
OVERLAY_SHOW_MS = 3_000         # title overlay before fade
MOUSE_IDLE_MS = 3_000           # cursor auto-hide

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
