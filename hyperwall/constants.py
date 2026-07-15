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
# The exe bundles a copy of the profile (hyperwall.spec datas) — a
# distributed exe with no loose .nip beside it silently skipped G-Sync
# isolation because only SCRIPT_DIR was consulted (2026-07-13 audit).
if getattr(sys, "frozen", False) and not os.path.exists(NIP_FILE):
    _bundled_nip = os.path.join(getattr(sys, "_MEIPASS", ""), "hyperwall.nip")
    if os.path.exists(_bundled_nip):
        NIP_FILE = _bundled_nip
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


def _s(px: int) -> int:
    """Scale a pixel metric by the configured UI scale (min 1px)."""
    return max(1, int(px * UI_SCALE))


# ── Timing ───────────────────────────────────────────────────────────────────
STREAM_START_STAGGER_MS = 300   # ms between cell starts
MAX_RETRIES = 3                 # then skip the dead stream
CONTROLS_HEIGHT = int(40 * UI_SCALE)  # px — single-row floating pill
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

# Systemic-outage guard: when a majority of cells (never fewer than
# OUTAGE_MIN_CELLS) record failures within OUTAGE_WINDOW_S, the cause is
# almost certainly shared (Emby wedged, network stall) — cells then back off
# for OUTAGE_BACKOFF_S and skip the transcode escalation instead of piling
# N concurrent transcode jobs onto a struggling server.
OUTAGE_WINDOW_S = _int_env("HYPERWALL_OUTAGE_WINDOW_S", 45, 10, 600)
OUTAGE_MIN_CELLS = _int_env("HYPERWALL_OUTAGE_MIN_CELLS", 3, 2, 100)
OUTAGE_BACKOFF_S = _int_env("HYPERWALL_OUTAGE_BACKOFF_S", 20, 5, 600)

# Direct-play budget: sources heavier than this transcode server-side. This is
# the ONLY auto-transcode gate — the >1080p resolution gate was dropped
# 2026-07-13 (A/B bench: 4K direct-plays with 0 drops, while the live-transcode
# arm produced unseekable, corruption-prone, stall-prone streams). A 1080p
# 120fps 96 Mbps file is still real decode + network load multiplied across
# the grid, hence the fps/bitrate caps. 0 disables a check.
MAX_DIRECT_FPS = _int_env("HYPERWALL_MAX_DIRECT_FPS", 66, 0, 1_000)
MAX_DIRECT_BITRATE_MBPS = _int_env("HYPERWALL_MAX_DIRECT_BITRATE_MBPS", 60, 0, 10_000)

# Usable server→client link budget (Mbps), used to divide the direct-play
# bitrate cap across cells (see effective_bitrate_budget_mbps). Default 800 is
# the practical ceiling of greg→skyhawk's 1 GbE link: ~940 Mbps TCP goodput at
# line rate, held to ~85% so concurrent readahead fill-bursts + other traffic
# (SSH, control) don't saturate it. Retune here (or via env) if that link
# changes — e.g. 2.5 GbE ≈ 2000, 10 GbE ≈ 8000.
LINK_MBPS = _int_env("HYPERWALL_LINK_MBPS", 800, 50, 100_000)


def effective_bitrate_budget_mbps(n_cells: int) -> int:
    """Cell-count-aware direct-play bitrate cap.

    An explicit HYPERWALL_MAX_DIRECT_BITRATE_MBPS wins verbatim. Otherwise
    the default 60 is scaled down as cells go up (8 cells → ~33 Mbps): the
    graduated middle ground between transcode-everything and
    direct-everything. The cap divides LINK_MBPS across cells with burst
    headroom, so the aggregate steady-state stays well inside the link (8
    cells × 33 Mbps ≈ 264 Mbps of an 800 Mbps budget). High-bitrate outliers
    transcode server-side, where Emby throttles delivery to ~realtime —
    smooth by construction — while the bulk of the library stays direct.
    ≤4 cells resolve to the base (measured clean at 4 cells).
    """
    if os.environ.get("HYPERWALL_MAX_DIRECT_BITRATE_MBPS"):
        return MAX_DIRECT_BITRATE_MBPS
    from .reliability import scale_bitrate_budget_mbps
    return scale_bitrate_budget_mbps(n_cells, MAX_DIRECT_BITRATE_MBPS, LINK_MBPS)

# Memory-aware demuxer cache budget. Each cell wants PER_CELL demuxer bytes, but
# the grid total is capped at CACHE_BUDGET_MB so large grids don't blow up RAM.
# Sized for this box (32 GB): 1 GB/cell, 8 GB grid cap — deep enough that the
# 60s readahead (see MPV_OPTS) is byte-bound only above ~140 Mbps, so network
# blips stay invisible. Was 512MB/3GB, tuned for a leaner host.
DEMUXER_PER_CELL_MB = _int_env("HYPERWALL_DEMUXER_PER_CELL_MB", 1_024, 32, 4_096)
CACHE_BUDGET_MB = _int_env("HYPERWALL_CACHE_BUDGET_MB", 8_192, 128, 65_536)

# ── MPV Options ──────────────────────────────────────────────────────────────
MPV_OPTS: dict[str, object] = dict(
    vo="gpu-next",
    gpu_api="d3d11",
    # d3d11va decodes straight into D3D11 textures that the gpu-next/d3d11
    # renderer consumes directly — no decode-surface copy back to system RAM
    # like nvdec-copy, and no CUDA↔D3D11 interop cost like non-copy nvdec.
    # Benchmarked on skyhawk (RTX 5070 Ti, 8-cell wall): ~30% lower VRAM
    # (~3.2 GB → ~2.2 GB) at equal CPU/power and zero frame drops. If a
    # driver/GPU regresses, override with HYPERWALL_HWDEC=nvdec-copy.
    hwdec="d3d11va",
    # High-quality downscaling. A video wall's core job is shrinking 4K/1080p
    # sources into small grid cells, so the downscaler IS the picture quality.
    # profile=fast used to force bilinear to save GPU this box doesn't need
    # (benchmark: ~1% GPU, 0 drops) — replaced with mitchell + correct/linear
    # downscaling (crisp, cheap), ewa_lanczossharp for the rare upscale, and
    # deband to kill gradient banding. Force a profile via HYPERWALL_PROFILE
    # if a weaker GPU ever runs this.
    dscale="mitchell",
    correct_downscaling="yes",
    linear_downscaling="yes",
    scale="ewa_lanczossharp",
    deband="yes",
    video_sync="audio",
    video_sync_max_video_change=5,
    interpolation="no",
    target_colorspace_hint="yes",
    cache="yes",
    # Resume only with 3s of buffer in hand: after a cache starvation
    # (network reset/stall) mpv otherwise resumes the instant one frame is
    # available and immediately starves again — a freeze-flicker loop. One
    # slightly longer pause beats three visible stutters (probed: option
    # accepted + read back).
    cache_pause="yes",
    cache_pause_wait=3,
    cache_secs=60,
    demuxer_max_bytes="1024MiB",
    demuxer_readahead_secs=60,
    network_timeout=15,
    stream_lavf_o="reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
    keep_open="always",
    # Open the queued playlist entry's demuxer once the current one is fully
    # read (≈ demuxer_readahead_secs before EOF) — the gapless-advance warmup
    # for the wall's prefetched next item (probed ~60ms to first frame).
    prefetch_playlist="yes",
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
    from .reliability import scale_demuxer_mb, scale_readahead_s

    out = dict(opts)
    mb = scale_demuxer_mb(
        n_cells,
        per_cell_mb=DEMUXER_PER_CELL_MB,
        total_budget_mb=CACHE_BUDGET_MB,
    )
    out["demuxer_max_bytes"] = f"{mb}MiB"
    # Readahead depth = burst size on every track open; scale it down with
    # cell count so 8 cells don't starve each other's steady reads
    # (2026-07-14: 80% of freezes began within 8s of a stream-open).
    base = int(out.get("demuxer_readahead_secs", 60) or 60)
    ra = scale_readahead_s(n_cells, base_s=base)
    out["demuxer_readahead_secs"] = ra
    out["cache_secs"] = min(int(out.get("cache_secs", 60) or 60), ra)
    return out
