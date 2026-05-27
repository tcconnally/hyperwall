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
NIP_FILE     = os.path.join(SCRIPT_DIR, "hyperwall.nip")
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
STREAM_START_STAGGER_MS = 2_000     # ms between cell starts — gives Emby transcode
# pipeline time to initialise before the next cell fires.  300 ms was too
# aggressive for ffmpeg probe + encoder init (500–1500 ms per transcode).
MAX_RETRIES             = 3         # then skip the dead stream
CONTROLS_HEIGHT         = 44        # px  — larger for better hit area + modern look
CONTROLS_OPACITY        = 0.82      # more opaque when visible, still translucent
AUTOHIDE_MS             = 5_000     # one-shot startup auto-hide
OVERLAY_SHOW_MS         = 3_000     # title overlay before fade
MOUSE_IDLE_MS           = 3_000     # cursor auto-hide

# ── MPV hardware tuning (Blackwell + 240 Hz UltraGear) ──────────────────────
# Monitor: LG 27" 240 Hz UltraGear (native 240 Hz, G-Sync Compatible, HDR400)
# GPU:    NVIDIA Blackwell (d3d11va zero-copy decode, d3d11 + gpu-next render)
# RAM:    32 GB system
#
# v8.2 production hardening (2026-05-23):
#   hwdec=nvdec → d3d11va    — zero-copy decode (no PCIe frame round-trip)
#   profile=fast  → removed  — was setting correct-pts=no, harming A/V sync
#   +d3d11-sync-interval=1   — enforce vsync on composited desktop
#   +d3d11-flip=yes          — flip-model presentation (lower latency)
#   demuxer 256MiB → 64MiB   — local LAN doesn't need huge read-ahead
#   audio_buffer 1.0 → 0.2   — reduced from 1s for faster mute/unmute
#   stagger 300ms → 2000ms   — give Emby transcode pipeline time to breathe
#
# v8.3 cyberpunk revamp (2026-05-25):
#   +demuxer-cache-background=yes — pre-fill cache before playback, reduces
#     initial stutter on multi-cell grids by having buffer headroom ready.
#   cache_secs 10 → env-overridable via HYPERWALL_CACHE_SECS
#   demuxer_max_bytes env-overridable via HYPERWALL_DEMUXER_MAX_BYTES
#   +audio-fallback-to-null       — prevent mpv from blocking on WASAPI
#     session exhaustion; makes cell count scalability explicit.
#
# v8.4 frame-pacing tuning (2026-05-27):
#   +video_sync_max_video_change=5 — cap per-frame correction at 5 ms.
#     Prevents large jump corrections after a dropped-frame burst that
#     would themselves cause visible stutter.
#   +correct_pts=yes — explicitly enforce correct PTS interpretation.
#     Was previously inherited from a removed profile=fast preset.
#   auto-transcode default 1 → 0 — on modest grids (≤8 cells) with a
#     modern GPU, hardware decoders handle most codecs natively. Retry
#     escalation catches failures automatically.
#   Classifier narrowed: HEVC 8-bit / AV1 8-bit ≤1080p → direct-play.
#   +d3d11_sync_interval env-overridable via HYPERWALL_D3D11_SYNC.
#   ao wasapi → null             — cells are muted; WASAPI shared-mode
#     contention across 4 instances causes audio underruns and cutting
#     out even when muted.  Set HYPERWALL_AO=wasapi to re-enable.
#
# Must stay in sync with deployed hardware and the principal-engineer audit.
MPV_OPTS = dict(
    vo                         = "gpu-next",
    gpu_api                    = "d3d11",
    hwdec                      = "d3d11va",
    d3d11_sync_interval        = 1,
    d3d11_flip                 = "yes",
    video_sync                 = "display-resample",
    video_sync_max_video_change = 5,
    correct_pts                 = "yes",
    interpolation              = "no",
    target_colorspace_hint     = "yes",
    cache                      = "yes",
    cache_secs                 = 10,
    demuxer_max_bytes          = "64MiB",
    demuxer_readahead_secs     = 10,
    demuxer_cache_background   = "yes",
    network_timeout            = 15,
    stream_lavf_o              = "reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
    keep_open                  = "always",
    force_window               = "no",
    idle                       = "yes",
    osd_level                  = 0,
    input_default_bindings     = False,
    input_vo_keyboard          = False,
    ytdl                       = False,
    ao                         = "null",
    audio_client_name          = "HyperWall",
    audio_buffer               = 0.2,
    audio_fallback_to_null     = "yes",
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
    if (v := os.environ.get("HYPERWALL_CACHE_SECS")) is not None:
        try:
            out["cache_secs"] = int(v)
        except ValueError:
            pass
    if (v := os.environ.get("HYPERWALL_DEMUXER_MAX_BYTES")) is not None:
        out["demuxer_max_bytes"] = v
    if (v := os.environ.get("HYPERWALL_DEMUXER_READAHEAD_SECS")) is not None:
        try:
            out["demuxer_readahead_secs"] = int(v)
        except ValueError:
            pass
    for var, key in (
        ("HYPERWALL_VO",          "vo"),
        ("HYPERWALL_HWDEC",       "hwdec"),
        ("HYPERWALL_GPU_API",     "gpu_api"),
        ("HYPERWALL_PROFILE",     "profile"),
        ("HYPERWALL_VIDEO_SYNC",  "video_sync"),
        ("HYPERWALL_AO",          "ao"),
        ("HYPERWALL_D3D11_SYNC",  "d3d11_sync_interval"),
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
