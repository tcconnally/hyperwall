import logging
import os
import sys
import platform
from logging.handlers import RotatingFileHandler

# ── Paths ─────────────────────────────────────────────────────────────────────
# When frozen by PyInstaller, sys.executable is hyperwall_v8.exe. In script mode,
# this package lives under <wall>/hyperwall, so the shared config/log/DLL directory
# is one level up.
if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
    LAUNCH_BASENAME = os.path.basename(sys.executable).lower()
else:
    SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    LAUNCH_BASENAME = os.path.basename(sys.executable).lower()

CONFIG_FILE  = os.path.join(SCRIPT_DIR, "config.ini")
LOG_FILE     = os.path.join(SCRIPT_DIR, "hyperwall.log")
LAUNCHER_EXE = os.path.join(SCRIPT_DIR, "hyperwall_v8.exe")
NIP_FILE     = os.path.join(SCRIPT_DIR, "hyperwall_v8.nip")
NPI_EXE      = os.path.join(SCRIPT_DIR, "tools", "nvidiaProfileInspector.exe")
NV_SENTINEL  = os.path.join(SCRIPT_DIR, ".hyperwall_v8_nvprofile.sentinel")

os.environ["PATH"] = SCRIPT_DIR + os.pathsep + os.environ.get("PATH", "")

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger("HyperWall")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")

def setup_logging(log_file: str):
    if not os.environ.get("HYPERWALL_NO_LOG_SETUP"):
        _fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        _fh.setFormatter(_fmt)
        logger.addHandler(_fh)
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setFormatter(_fmt)
    logger.addHandler(_ch)

# ── Tuning constants ──────────────────────────────────────────────────────────
STREAM_START_STAGGER_MS = 300       # ms between cell starts
MAX_RETRIES             = 3         # then skip the dead stream
CONTROLS_HEIGHT         = 36        # px
CONTROLS_OPACITY        = 0.65
AUTOHIDE_MS             = 5_000     # one-shot startup auto-hide
OVERLAY_SHOW_MS         = 3_000     # title overlay before fade
MOUSE_IDLE_MS           = 3_000     # cursor auto-hide

# mpv tuning — locked to the hardware spec.
MPV_OPTS = dict(
    vo                         = "gpu-next",
    gpu_api                    = "d3d11",


    hwdec                      = "nvdec-copy",
    profile                    = "fast",
    video_sync                 = "audio",
    interpolation              = "no",
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
    ao                         = "wasapi",
    audio_client_name          = "HyperWall",
    audio_buffer               = 1.0,
    msg_level                  = "all=warn,cplayer=info,ao=error,ao/wasapi=fatal",
)

# Cell-specific tuning constants
HISTORY_MAXLEN = 50
PLAYED_ANYTHING_THRESHOLD = 0.05
MIN_NEXT_INTERVAL_S = 0.75
SEEK_SLIDER_MAX = 1000
SEEK_FRACTION_MAX = 0.90

# Controller-specific tuning constants
API_POOL_MAX_WORKERS = 4

# Transcoding profile parameters for Emby stream URLs
TRANSCODE_VIDEO_CODEC    = "h264"
TRANSCODE_AUDIO_CODEC    = "aac"
TRANSCODE_AUDIO_CHANNELS = 2
TRANSCODE_MAX_HEIGHT     = 1080
TRANSCODE_MAX_WIDTH      = 1920
TRANSCODE_MAX_FRAMERATE  = 30
TRANSCODE_VIDEO_BITRATE  = 12000000

def _get_platform_specific_mpv_opts():
    if platform.system() == "Windows":
        return {
            "gpu_api": "d3d11",
            "hwdec":   "nvdec-copy",
            "ao":      "wasapi",
            "vo":      "gpu-next",
        }
    elif platform.system() == "Linux":
        return {
            "gpu_api": "opengl",  # or "vulkan" if preferred/available
            "hwdec":   "auto-copy", # mpv will try vaapi, vdpau, etc.
            "ao":      "pulse",   # or "alsa"
            "vo":      "gpu",
        }
    elif platform.system() == "Darwin": # macOS
        return {
            "gpu_api": "opengl",
            "hwdec":   "videotoolbox",
            "ao":      "coreaudio",
            "vo":      "gpu",
        }
    return {} # Default for unknown OS

_PLATFORM_OPTS = _get_platform_specific_mpv_opts()

# Apply platform-specific defaults, but allow MPV_OPTS to override
MPV_OPTS.update(_PLATFORM_OPTS)


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

def apply_perf_env(opts: dict) -> dict:
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

_MPV_LOG_NOISE = (
    "UDTA parsing failed retrying raw",
    "Detected creation time before 1970",
    "Unknown cover type",
    "stream 0, timescale not set",
    "client removed during hook handling",
    "Immediate exit requested",
    "Leaking 1 nested connections",
)
