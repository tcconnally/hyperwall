import logging
import os
import shutil
import sys
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
# Resilient fallback: relative tools/, NPI_PATH, Program Files, Downloads, ~, PATH via shutil.which.
# Removes fragile/hardcoded user-specific paths; works across installs and build modes.
if not os.path.exists(NPI_EXE):
    search_dirs = [
        os.environ.get("NPI_PATH", ""),
        os.environ.get("PROGRAMFILES", ""),
        os.environ.get("PROGRAMFILES(X86)", ""),
        os.path.expanduser(r"~\Downloads"),
        os.path.expanduser("~"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    ]
    for base in search_dirs:
        if not base:
            continue
        for sub in ("", "tools", "bin"):
            cand = os.path.join(base, sub, "nvidiaProfileInspector.exe")
            if os.path.exists(cand):
                NPI_EXE = cand
                break
        else:
            continue
        break
    else:
        found = shutil.which("nvidiaProfileInspector.exe") or shutil.which("nvidiaProfileInspector")
        if found:
            NPI_EXE = found
NV_SENTINEL  = os.path.join(SCRIPT_DIR, ".hyperwall_v8_nvprofile.sentinel")

# ── Logging ───────────────────────────────────────────────────────────────────
logger = logging.getLogger("HyperWall")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")

class MPVLogFilter(logging.Filter):
    def filter(self, record):
        if "mpv[" in record.msg and any(pat in record.msg for pat in _MPV_LOG_NOISE):
            return False
        return True

def setup_logging(log_file: str):
    if not os.environ.get("HYPERWALL_NO_LOG_SETUP"):
        _fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        _fh.setFormatter(_fmt)
        _fh.addFilter(MPVLogFilter()) # Add the custom filter here
        logger.addHandler(_fh)
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setFormatter(_fmt)
    _ch.addFilter(MPVLogFilter()) # Add the custom filter here
    logger.addHandler(_ch)

# ── Tuning constants ──────────────────────────────────────────────────────────
STREAM_START_STAGGER_MS = 300       # ms between cell starts
MAX_RETRIES             = 3         # then skip the dead stream
CONTROLS_HEIGHT         = 44        # px  — larger for better hit area + modern look
CONTROLS_OPACITY        = 0.82      # more opaque when visible, still translucent
AUTOHIDE_MS             = 5_000     # one-shot startup auto-hide
OVERLAY_SHOW_MS         = 3_000     # title overlay before fade
MOUSE_IDLE_MS           = 3_000     # cursor auto-hide

# ── MPV hardware tuning (Blackwell B200/B100 series + 240 Hz UltraGear) ──────
# Monitor: LG 27" 240 Hz UltraGear (native 240 Hz, G-Sync Compatible, HDR400)
# GPU:    NVIDIA Blackwell (nvdec / CUDA offload, d3d11 + gpu-next)
# RAM:    32 GB system (generous demuxer/cache to absorb 4K remux bursts)
# Values below are chosen for low-latency 240 Hz playback with HDR hinting.
# Must stay in sync with INSTRUCTIONS_v8.md and actual deployed hardware.
MPV_OPTS = dict(
    vo                         = "gpu-next",
    gpu_api                    = "d3d11",
    hwdec                      = "nvdec",
    profile                    = "fast",
    video_sync                 = "display-resample",
    interpolation              = "no",
    target_colorspace_hint     = "yes",
    cache                      = "yes",
    cache_secs                 = 10,
    demuxer_max_bytes          = "256MiB",
    demuxer_readahead_secs     = 20,
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
