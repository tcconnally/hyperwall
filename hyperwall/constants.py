"""Hyperwall's macOS-native runtime and playback tuning constants."""

from __future__ import annotations

import os
import sys

from .macos_runtime import decoder_for_profile

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCH_BASENAME = os.path.basename(sys.executable).lower()

CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.ini")
LOG_FILE = os.path.join(SCRIPT_DIR, "hyperwall.log")

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


# Serialize queued demuxer starts so eight cells do not fill their caches at
# the same instant. 0 is an explicit diagnostic escape hatch.
PREFETCH_MIN_INTERVAL_MS = _int_env(
    "HYPERWALL_PREFETCH_INTERVAL_MS", 500, 0, 5_000
)


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
SESSION_CLEANUP_RETRY_S = _int_env("HYPERWALL_SESSION_CLEANUP_RETRY_S", 5, 1, 120)

# Per-cell media fault containment. Hardware decoder errors get one local
# software fallback before a repeated software fault quarantines the item;
# transport failures get one retry before advancing past the resource.
DECODER_FAULT_MAX = _int_env("HYPERWALL_DECODER_FAULT_MAX", 2, 1, 8)
TRANSPORT_RETRY_MAX = _int_env("HYPERWALL_TRANSPORT_RETRY_MAX", 1, 0, 3)

# Starvation fault gate (2026-08-08 soak): tracks that repeatedly run the
# cache dry (repeat offenders froze up to 15x per run; single starvations of
# 9-11s during server hiccups) are treated as media faults once they cross
# either threshold — advance past the resource instead of stuttering to EOF.
STARVATION_FAULT_EVENTS = _int_env("HYPERWALL_STARVATION_FAULT_EVENTS", 3, 2, 10)
STARVATION_FAULT_TOTAL_S = _int_env("HYPERWALL_STARVATION_FAULT_S", 20, 5, 120)

# Transcode prefetch deferral (2026-08-09 soak follow-up): when every
# transcode slot is busy the prefetch is requeued and retried on a timer
# instead of being dropped (a dropped prefetch cold-starts at advance →
# cache starvation). 0 disables either knob, restoring the old skip.
TRANSCODE_PREFETCH_RETRY_S = _int_env("HYPERWALL_TRANSCODE_PREFETCH_RETRY_S", 8, 0, 120)
TRANSCODE_PREFETCH_RETRY_ATTEMPTS = _int_env("HYPERWALL_TRANSCODE_PREFETCH_RETRY_ATTEMPTS", 4, 0, 24)

def _physical_memory_mb() -> int | None:
    """Return host physical memory without spawning a platform command."""
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        if pages > 0 and page_size > 0:
            return (pages * page_size) // (1024 * 1024)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return None


def macos_cache_defaults(physical_memory_mb: int | None = None) -> tuple[int, int]:
    """Choose a bounded cache for an M-series MacBook Air.

    The 16 GiB M5 soak showed cache starvation at 128 MiB/cell. Keep the
    measured 256 MiB/cell floor and a 2 GiB aggregate ceiling through 24 GiB.
    Larger Macs get a modest ceiling increase, never the old desktop-wide
    8 GiB default.
    """
    memory_mb = (
        _physical_memory_mb() if physical_memory_mb is None else physical_memory_mb
    )
    if memory_mb is None or memory_mb <= 24 * 1024:
        return 256, 2_048
    return 512, 4_096


def stable_direct_profile(
    n_cells: int = 0,
    override: str | None = None,
) -> bool:
    """Return whether the explicit fail-closed direct-only escape is enabled.

    The previous automatic M5/8-cell default removed heavy and unmeasured
    resources from the library and disabled the server H.264/AAC path. That
    made the wall look stable by silently hiding content. Normal playback now
    retains the complete library and uses the bounded auto-transcode planner;
    the direct-only pool remains available only as an explicit emergency
    override via ``HYPERWALL_STABLE_DIRECT_ONLY=1``.

    ``n_cells`` remains part of the call shape for future profile limits, but
    does not silently change the default.
    """
    raw = (
        os.environ.get("HYPERWALL_STABLE_DIRECT_ONLY")
        if override is None else override
    )
    if raw is not None:
        value = str(raw).strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
    return False


STABLE_DIRECT_MAX_FPS = _int_env("HYPERWALL_STABLE_MAX_FPS", 30, 1, 240)
STABLE_DIRECT_MAX_BITRATE_MBPS = _int_env(
    "HYPERWALL_STABLE_MAX_BITRATE_MBPS", 20, 1, 1_000
)


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

# Max cells that may transcode simultaneously. Greg's Arc A310 media engine
# handles a few concurrent 1080p transcodes; the governor prevents a cold-start
# stampede. When the cap is full, a normal auto-transcode admission is bounded
# by the controller rather than turning the library into a filtered list.
MAX_CONCURRENT_TRANSCODES = _int_env("HYPERWALL_MAX_CONCURRENT_TRANSCODES", 4, 0, 64)


def effective_bitrate_budget_mbps(n_cells: int) -> int:
    """Cell-count-aware direct-play bitrate cap.

    An explicit HYPERWALL_MAX_DIRECT_BITRATE_MBPS wins verbatim. Otherwise
    the default 60 is scaled down as cells go up (8 cells → ~50 Mbps): the
    graduated middle ground between transcode-everything and
    direct-everything. The cap divides LINK_MBPS across cells with burst
    headroom, so the aggregate steady-state stays inside the link (8
    cells × 50 Mbps ≈ 400 Mbps of an 800 Mbps budget). Heavy outliers
    transcode server-side (capped in count by MAX_CONCURRENT_TRANSCODES);
    the bulk of the library stays direct — the reliable path on this setup.
    ≤4 cells resolve to the base (measured clean at 4 cells).
    """
    if os.environ.get("HYPERWALL_MAX_DIRECT_BITRATE_MBPS"):
        return MAX_DIRECT_BITRATE_MBPS
    from .reliability import scale_bitrate_budget_mbps
    return scale_bitrate_budget_mbps(n_cells, MAX_DIRECT_BITRATE_MBPS, LINK_MBPS)

# Memory-aware demuxer cache budget. Each cell wants PER_CELL demuxer bytes, but
# the grid total is capped at CACHE_BUDGET_MB so a MacBook Air does not trade
# GUI responsiveness for a desktop-sized media cache.
_DEFAULT_DEMUXER_MB, _DEFAULT_CACHE_BUDGET_MB = macos_cache_defaults()
DEMUXER_PER_CELL_MB = _int_env(
    "HYPERWALL_DEMUXER_PER_CELL_MB", _DEFAULT_DEMUXER_MB, 32, 4_096
)
CACHE_BUDGET_MB = _int_env(
    "HYPERWALL_CACHE_BUDGET_MB", _DEFAULT_CACHE_BUDGET_MB, 128, 65_536
)

# ── MPV Options ──────────────────────────────────────────────────────────────
_MPV_OPTS_BASE: dict[str, object] = dict(
    vo="libmpv",
    hwdec="no",
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
    # Cover each cell edge-to-edge. Keep the source aspect ratio, but crop
    # overflow rather than leaving black bars in portrait/narrow grids.
    panscan=1.0,
    video_sync="display-resample",
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
    network_timeout=25,
    stream_lavf_o="reconnect=1,reconnect_streamed=1,reconnect_delay_max=10",
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
    ao="coreaudio,null",
    audio_buffer=2.0,
    msg_level="all=warn,cplayer=info,ao=error,ao/coreaudio=fatal",
)

# ── macOS render options ─────────────────────────────────────────────────────
def macos_mpv_opts(
    *,
    profile: str | None = None,
    physical_memory_mb: int | None = None,
) -> dict[str, object]:
    """Return the single native render configuration used by Hyperwall.

    ``physical_memory_mb`` remains an accepted probe argument for deterministic
    callers, but it deliberately does not choose a decoder. M5 decoder choice
    is an explicit profile so a target-host pilot can change one variable at a
    time without a hidden RAM heuristic.
    """
    del physical_memory_mb
    out = dict(_MPV_OPTS_BASE)
    out["vo"] = "libmpv"
    out["hwdec"] = decoder_for_profile(profile)
    out["video_sync"] = "display-resample"
    out["ao"] = "coreaudio,null"
    out["video_timing_offset"] = 0
    return out


# ── Display roles ────────────────────────────────────────────────────────────
class DisplayRole:
    """Role assigned to each selected monitor at launch.

    WALL      — the public video wall (e.g. 2×2 on an external display).
    PREVIEW   — a larger operator grid (e.g. 3×4) for browsing; any cell can
                be double-clicked to go full-screen on that laptop while the
                wall grid keeps playing.
    """

    WALL = "wall"
    PREVIEW = "preview"

    _ALL = (WALL, PREVIEW)

    @classmethod
    def is_valid(cls, value: str | None) -> bool:
        return value in cls._ALL


class DisplayRotation:
    """Per-display orientation preference captured by the setup wizard.

    ``AUTO`` follows the physical orientation reported by the operating
    system. The explicit degree values describe the monitor's intended
    clockwise rotation and are persisted as strings so the config remains
    stable across JSON/config-parser round trips.
    """

    AUTO = "auto"
    DEG_0 = "0"
    DEG_90 = "90"
    DEG_180 = "180"
    DEG_270 = "270"

    _ALL = (AUTO, DEG_0, DEG_90, DEG_180, DEG_270)

    @classmethod
    def is_valid(cls, value: str | None) -> bool:
        return value in cls._ALL


def normalize_display_layout(raw: object | None) -> dict[str, object]:
    """Return a safe, typed per-display rotation + grid configuration.

    Config files are user-editable, so malformed entries must not prevent the
    wall from starting. Grid dimensions are constrained to the same 1..6
    range exposed by the wizard; an invalid rotation falls back to AUTO.
    """
    data = raw if isinstance(raw, dict) else {}

    rotation = str(data.get("rotation", DisplayRotation.AUTO)).strip().lower()
    if not DisplayRotation.is_valid(rotation):
        rotation = DisplayRotation.AUTO

    def _dimension(key: str) -> int:
        try:
            value = int(data.get(key, 2))
        except (TypeError, ValueError):
            value = 2
        return max(1, min(6, value))

    return {
        "rotation": rotation,
        "rows": _dimension("rows"),
        "cols": _dimension("cols"),
    }


_MACOS_RENDER_PROFILES: dict[str, dict[str, object]] = {
    "hq": {
        "dscale": "mitchell",
        "correct_downscaling": "yes",
        "linear_downscaling": "yes",
        "scale": "ewa_lanczossharp",
        "deband": "yes",
    },
    # Explicit diagnostic/operational tier for thermally constrained Macs.
    # It avoids mpv's broad profile=fast switch so other playback options and
    # platform defaults remain unchanged.
    "low-cost": {
        "dscale": "bilinear",
        "correct_downscaling": "no",
        "linear_downscaling": "no",
        "scale": "bilinear",
        "deband": "no",
    },
}


def apply_render_profile(
    opts: dict,
    profile: str | None = None,
) -> dict:
    """Apply an explicitly named macOS render tier to an options copy.

    ``HYPERWALL_RENDER_PROFILE`` is intentionally separate from mpv's
    ``HYPERWALL_PROFILE`` option. Unknown profiles are a no-op.
    """
    out = dict(opts)
    selected = profile
    if selected is None:
        selected = os.environ.get("HYPERWALL_RENDER_PROFILE", "hq")
    selected = str(selected or "hq").strip().lower()
    values = _MACOS_RENDER_PROFILES.get(selected)
    if values is not None:
        out.update(values)
    return out


MPV_OPTS: dict[str, object] = macos_mpv_opts()

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
    # Render/display timing and audio-clock fields make the final stats useful
    # for distinguishing VO pressure from audio or display synchronization.
    "display-sync-active",
    "vsync-ratio",
    "avsync",
    "total-avsync-change",
    "audio-params",
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
        ("HYPERWALL_HWDEC", "hwdec"),
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
    return apply_render_profile(out)


def apply_cache_budget(
    opts: dict,
    n_cells: int,
    *,
    physical_memory_mb: int | None = None,
) -> dict:
    """Return a copy of opts with demuxer_max_bytes scaled to the cell count.

    Keeps the aggregate grid demuxer buffer within CACHE_BUDGET_MB so large
    grids (e.g. 6x6) don't exhaust RAM. Pure w.r.t. the reliability helper.

    ``physical_memory_mb`` lets tests and diagnostic tooling evaluate another
    Mac memory target without mutating process-wide environment state.
    """
    from .reliability import scale_demuxer_mb, scale_readahead_s

    out = dict(opts)
    if physical_memory_mb is None:
        per_cell_mb = DEMUXER_PER_CELL_MB
        total_budget_mb = CACHE_BUDGET_MB
    else:
        per_cell_mb, total_budget_mb = macos_cache_defaults(physical_memory_mb)
    mb = scale_demuxer_mb(
        n_cells,
        per_cell_mb=per_cell_mb,
        total_budget_mb=total_budget_mb,
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
