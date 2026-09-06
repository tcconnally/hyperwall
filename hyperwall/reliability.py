"""
Hyperwall — pure reliability helpers (no PyQt / mpv / Emby imports).

The 24/7-wall self-healing logic lives here as small, side-effect-free
functions so it can be unit-tested without a display server, an mpv build, or
a live Emby instance. `cell.py` imports these; the wiring (QTimers, mpv calls,
overlays) stays in the widget while the *decisions* are verifiable in isolation.

Six concerns (Epic 2 / #7 + the 2026-07-11 stampede/lockup reviews):
  - stall detection      → is_stalled()
  - crash-loop parking   → count_recent() / should_park()
  - cache-budget scaling → scale_demuxer_mb()
  - retry desync         → apply_jitter()
  - outage detection     → is_systemic_outage()
  - mpv event decoding   → end_file_reason()
  - starvation faults    → starvation_fault_reached()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class PlaybackToken:
    """Immutable identity for one mpv/resource playback generation."""

    mpv_generation: int
    track_generation: int
    item_id: str | None
    stream_url: str


def playback_token_is_current(
    token: PlaybackToken,
    *,
    mpv_generation: int,
    track_generation: int,
    item_id: str | None,
    stream_url: str | None,
    closing: bool,
) -> bool:
    """Reject callbacks for replaced resources or a closing cell."""
    return (
        not closing
        and stream_url is not None
        and token.mpv_generation == mpv_generation
        and token.track_generation == track_generation
        and token.item_id == item_id
        and token.stream_url == stream_url
    )

def is_stalled(
    idle_s: float,
    *,
    paused: bool,
    dragging: bool,
    threshold_s: float,
) -> bool:
    """Decide whether a cell has silently stalled (frozen mid-stream).

    A stall is "no forward time-pos progress for longer than threshold_s while
    the cell is actively trying to play." Paused/seeking cells never count —
    their lack of progress is intentional, not a fault.
    """
    if paused or dragging:
        return False
    return idle_s > threshold_s


def count_recent(times: Iterable[float], now: float, window_s: float) -> int:
    """Count timestamps within the trailing window ending at `now`."""
    return sum(1 for t in times if 0 <= now - t <= window_s)


def should_park(
    times: Iterable[float],
    now: float,
    *,
    window_s: float,
    threshold: int,
) -> bool:
    """True when failures within the rolling window reach the park threshold.

    Distinguishes a single bad item (skip and move on) from a systemic outage
    (e.g. Emby down) where a cell would otherwise hammer the server forever.
    """
    return count_recent(times, now, window_s) >= threshold


def outage_recovery_plan(retry_count: int, max_retries: int) -> dict[str, str]:
    """Choose whether a systemic-outage retry should continue or park."""
    if retry_count <= max(0, max_retries):
        return {"action": "retry"}
    return {"action": "park"}


def scale_demuxer_mb(
    n_cells: int,
    *,
    per_cell_mb: int,
    total_budget_mb: int,
    floor_mb: int = 32,
) -> int:
    """Per-cell demuxer budget (MiB), scaled so the grid total stays bounded.

    Each cell wants `per_cell_mb`, but N cells must not exceed
    `total_budget_mb` in aggregate (a 6x6 grid at 512 MiB/cell would reach
    ~18 GB). Returns min(per_cell_mb, budget/N) clamped to `floor_mb`.
    """
    n = max(1, int(n_cells))
    per = min(per_cell_mb, total_budget_mb / n)
    floor = max(0, int(floor_mb))
    if floor * n <= total_budget_mb:
        return int(max(floor, per))
    # When an explicit budget is smaller than the nominal floor for every
    # cell, the aggregate ceiling wins. This keeps low-budget overrides from
    # silently multiplying back above the caller's limit.
    return max(1, int(per))


def scale_readahead_s(n_cells: int, base_s: int = 60, floor_s: int = 10) -> int:
    """Per-cell demuxer readahead (seconds), scaled down as cells go up.

    Readahead depth is burst SIZE: on every track open, mpv fills it at
    line rate. Eight cells each slurping 60s of a 20 Mbps stream produce
    fill-bursts that starve the other cells' steady reads — measured
    2026-07-14: 80% of freeze episodes began within 8s of a stream-open.
    <=4 cells keep the full depth (measured clean); beyond that the depth
    shrinks so aggregate burst demand stays roughly constant.
    """
    n = max(1, int(n_cells))
    if n <= 4:
        return base_s
    return max(floor_s, int(base_s * 4 / n))


def prefetch_slot(
    now: float,
    next_ready: float,
    *,
    interval_s: float,
) -> tuple[float, float]:
    """Reserve the next serialized prefetch-start slot.

    Returns ``(delay_s, new_next_ready)``. The calculation is pure so the
    controller can pace burst starts without sleeping or blocking Qt; callers
    must revalidate their playback token when the delayed slot fires.
    """
    current = float(now)
    ready = max(current, float(next_ready))
    return max(0.0, ready - current), ready + max(0.0, float(interval_s))


def scale_bitrate_budget_mbps(
    n_cells: int, base_mbps: int, link_mbps: int = 800,
) -> int:
    """Cell-count-aware direct-play bitrate cap (the graduated middle
    ground between transcode-everything and direct-everything).

    Items above the cap transcode server-side. The cap divides the usable
    link (`link_mbps`) across cells with 2x burst headroom; at <=4 cells it
    resolves to the configured base (measured clean), at 8 cells ~50 Mbps so
    only genuinely heavy 50-60+ Mbps outliers transcode while the bulk of the
    library stays direct.

    The headroom was relaxed 3x->2x (33->50 Mbps at 8 cells, 2026-07-15):
    the original 33 sent ~6 of 8 startup cells to transcode at once and
    stampeded greg's media engine (HTTP 500s + partial, pixelated segments).
    Fewer files now transcode, and a concurrency gate (see gate_auto_transcode)
    caps how many run at once — direct-play is the reliable path on this setup.

    `link_mbps` defaults to 800 (greg->skyhawk 1 GbE usable goodput);
    callers pass constants.LINK_MBPS so it retunes with the real link.
    """
    n = max(1, int(n_cells))
    return min(base_mbps, max(8, link_mbps // (2 * n)))


def gate_auto_transcode(
    want_auto: bool, active_transcodes: int, max_concurrent: int,
) -> bool:
    """Whether an AUTO transcode should proceed given how many cells are
    already transcoding.

    Forced retries (a file that failed direct play and MUST transcode) bypass
    this in the caller. Protects greg's Arc A310 / QuickSync media engine from
    a cold-start stampede — 8 cells escalating at once produced HTTP 500s and
    partial, pixelated HLS segments (2026-07-15). Over the cap, the heavy clip
    direct-plays instead (reliable). max_concurrent <= 0 disables the gate.
    """
    if not want_auto:
        return False
    if max_concurrent <= 0:
        return True
    return active_transcodes < max_concurrent


def is_transcode_stream(url: str | None) -> bool:
    """Return whether ``url`` is an Emby server-transcode HLS master."""
    return bool(url) and "/master.m3u8" in url.lower()


def transcode_load_count(
    streams: Iterable[tuple[str | None, bool]],
) -> int:
    """Count current and queued server-transcode HLS streams."""
    return sum(1 for url, _is_prefetch in streams if is_transcode_stream(url))


def active_transcode_count(
    streams: Iterable[tuple[str | None, bool]],
) -> int:
    """Count currently playing HLS transcodes, excluding queued prefetches."""
    return transcode_load_count(
        (url, is_prefetch)
        for url, is_prefetch in streams
        if not is_prefetch
    )


def allow_transcode_prefetch(
    active_transcodes: int,
    max_concurrent: int,
    pending_transcodes: int = 0,
) -> bool:
    """Whether warming another server-transcode playlist is safe."""
    return (
        max_concurrent <= 0
        or active_transcodes + pending_transcodes < max_concurrent
    )


def apply_jitter(delay_s: float, rand: float) -> float:
    """Spread a retry delay over [0.75x, 1.25x] to desynchronize cells.

    Every cell computes the same escalation_plan delays, so a wall-wide fault
    (Emby hiccup, network stall) makes all cells retry at the same instant —
    a thundering herd against an already-struggling server. `rand` is an
    injected uniform sample in [0, 1) (pass random.random()) so this stays
    pure and deterministic under test.
    """
    r = min(1.0, max(0.0, float(rand)))
    return delay_s * (0.75 + 0.5 * r)


def is_systemic_outage(
    events: Iterable[tuple[float, object]],
    now: float,
    *,
    window_s: float,
    total_cells: int,
    min_cells: int = 3,
) -> bool:
    """True when enough *distinct* cells failed recently to imply a shared
    cause (server/network outage) rather than per-item bad media.

    `events` is an iterable of (timestamp, cell_key) failure records. The
    trigger is a majority of the wall, but never fewer than `min_cells`, so
    small walls (1–2 cells) keep plain per-cell escalation — with that few
    cells, "systemic vs bad file" is indistinguishable anyway.

    Callers use this to switch strategy: back off longer and *don't* escalate
    to server transcode (piling N concurrent transcode jobs onto a server
    that's already stalling makes the outage worse — observed 2026-07-11).
    """
    if total_cells < min_cells:
        return False
    recent = {
        key for t, key in events if 0 <= now - t <= window_s
    }
    needed = max(min_cells, (total_cells + 1) // 2)
    return len(recent) >= needed


# Keep these markers deliberately narrow. They are used on mpv log text from
# multiple codecs/backends, so a generic "error" must never trigger a player
# recreation on its own.
_MALFORMED_STREAM_MARKERS = (
    "data partitioning is not implemented",
    "moov atom not found",
    "failed to recognize file format",
    "vps 0 does not exist",
    "sps 0 does not exist",
    "non-existing sps",
    "non-existing pps",
    "bytestream",
)
_DECODER_FAULT_MARKERS = (
    "hardware accelerator failed",
    "vt decoder cb: output image buffer is null",
    "error while decoding",
    "missing reference picture",
    "co located pocs",
    "invalid nal unit",
    *_MALFORMED_STREAM_MARKERS,
)
_TRANSPORT_FAULT_MARKERS = (
    "no route to host",
    "network is unreachable",
    "connection reset",
    "connection refused",
    "connection timed out",
    "operation timed out",
    "partial file",
    "failed to seek",
    "seek failed",
    "http error 5",
    "http 5",
)


def classify_playback_fault(message: str) -> str:
    """Classify a narrow mpv/FFmpeg fault for per-cell recovery.

    The caller supplies a single log message. Unknown messages remain
    ``"other"`` so ordinary codec warnings, audio underruns, and informational
    lines cannot accidentally reset a live player.
    """
    text = str(message or "").lower()
    if any(marker in text for marker in _DECODER_FAULT_MARKERS):
        return "decoder"
    if any(marker in text for marker in _TRANSPORT_FAULT_MARKERS):
        return "transport"
    return "other"


def audio_track_for_mute(muted: bool) -> str:
    """Return the mpv audio-track selection for the requested mute state.

    Muting a cell must stop its audio demuxer, not merely silence the output.
    The wall deliberately allows only one audible cell, so leaving ``aid=auto``
    on previously unmuted cells multiplies decode and cache pressure.
    """
    return "no" if bool(muted) else "auto"


def is_malformed_stream_fault(message: str) -> bool:
    """Return whether a decoder log identifies unrecoverable media bytes."""
    text = str(message or "").lower()
    return any(marker in text for marker in _MALFORMED_STREAM_MARKERS)


def is_prefetch_fault(
    fault: str,
    message: str,
    *,
    has_prefetch: bool,
    switching: bool,
) -> bool:
    """Whether an unscoped log is a malformed queued-prefetch fault."""
    return (
        fault == "decoder"
        and has_prefetch
        and not switching
        and is_malformed_stream_fault(message)
    )


def context_for_prefetch_fault(
    fault: str,
    message: str,
    prefetch_context: tuple[object, ...] | None,
    *,
    generation: int,
    switching: bool,
) -> tuple[object, ...] | None:
    """Return a queued-resource context only for a safe prefetch fault."""
    if not is_prefetch_fault(
        fault, message, has_prefetch=prefetch_context is not None,
        switching=switching,
    ):
        return None
    if prefetch_context is None:
        return None
    try:
        if prefetch_context[0] != generation:
            return None
    except (IndexError, TypeError):
        return None
    return prefetch_context


def context_for_unscoped_fault(
    fault: str,
    active_context: tuple[object, ...] | None,
    *,
    generation: int,
    switching: bool,
) -> tuple[object, ...] | None:
    """Return the active resource for a fault with no event identity.

    mpv log callbacks carry the mpv generation but not the playlist entry.
    Attribution is therefore safe only while a matching active context exists
    and the cell is not between playlist resources. Unknown messages and stale
    generations remain unscoped rather than being sent into recovery.
    """
    if fault not in {"decoder", "transport"} or switching:
        return None
    if active_context is None:
        return None
    try:
        if active_context[0] != generation:
            return None
    except (IndexError, TypeError):
        return None
    return active_context


def decoder_recovery_plan(
    fault_count: int,
    *,
    hardware_decode: bool,
    max_faults: int = 2,
    malformed_stream: bool = False,
) -> dict[str, object]:
    """Choose a bounded per-cell response to decoder faults.

    The first hardware-decoder fault switches only that cell to software
    decoding and recreates its mpv instance. Software faults get one fresh
    demuxer retry; repeated software faults quarantine the current item.
    """
    count = max(1, int(fault_count))
    limit = max(1, int(max_faults))
    if hardware_decode:
        return {"action": "fallback-software", "hwdec": "no"}
    if malformed_stream:
        return {"action": "skip", "hwdec": "no"}
    if count < limit:
        return {"action": "recreate", "hwdec": "no"}
    return {"action": "skip", "hwdec": "no"}


def transport_recovery_plan(attempt: int, *, max_attempts: int = 1) -> dict[str, object]:
    """Retry one failed resource, then let the caller advance past it."""
    if int(attempt) <= max(0, int(max_attempts)):
        return {"action": "retry", "delay_s": 3}
    return {"action": "skip", "delay_s": 0}


# libmpv MPV_END_FILE_REASON_* values, as surfaced by python-mpv's
# MpvEventEndFile.reason when only the int is available.
_END_FILE_REASONS = {
    0: "eof",
    1: "restarted",
    2: "stop",
    3: "quit",
    4: "error",
    5: "redirect",
}


def end_file_reason(ev: object) -> str:
    """Extract the canonical end-file reason string from a python-mpv event.

    Duck-typed across python-mpv API generations (probed live 2026-07-12
    against the shipped mpv-2.dll + python-mpv 1.x):
      - 1.x: ev.as_dict() → {"reason": b"stop"/b"error"/...} (bytes!)
      - 1.x: ev.data → MpvEventEndFile with .reason as an int
      - legacy: ev.event → plain dict with a "reason" key

    The old inline extraction did `ev.event.get("reason")`, which raises
    AttributeError on python-mpv 1.x and silently defaulted EVERY event —
    including load errors and loadfile replaces — to "eof". Unknown shapes
    still fall back to "eof" (the historic default) so a future API change
    degrades to the old behavior rather than crashing the event thread.
    """
    def _norm(raw: object) -> str | None:
        if isinstance(raw, bytes):
            return raw.decode("utf-8", "replace")
        if isinstance(raw, bool):  # bools are ints; reject explicitly
            return None
        if isinstance(raw, int):
            return _END_FILE_REASONS.get(raw, "eof")
        if isinstance(raw, str) and raw:
            return raw
        return None

    try:
        got = _norm(ev.as_dict().get("reason"))  # type: ignore[attr-defined]
        if got is not None:
            return got
    except Exception:
        pass
    try:
        got = _norm(getattr(getattr(ev, "data", None), "reason", None))
        if got is not None:
            return got
    except Exception:
        pass
    try:
        legacy = getattr(ev, "event", None)
        if isinstance(legacy, dict):
            got = _norm(legacy.get("reason"))
            if got is not None:
                return got
    except Exception:
        pass
    return "eof"


def escalation_plan(attempt: int, max_retries: int) -> dict:
    """Decide what to do after a playback failure at 1-based `attempt`.

    Returns a small plan dict describing the retry/escalation policy, so the
    behavior is testable without QTimer/mpv:

      - action: "retry" (try again) or "skip" (give up, advance playlist)
      - transcode: True once attempt >= 2 (escalate to server transcode)
      - delay_s: backoff before the next attempt (2**attempt for retries, 0
                 for a skip)

    attempt 1 → retry direct (2s), attempt 2 → retry transcode (4s),
    attempt 3 → retry transcode (8s), attempt 4 (> max_retries=3) → skip.
    """
    if attempt <= max_retries:
        return {
            "action": "retry",
            "transcode": attempt >= 2,
            "delay_s": 2 ** attempt,
        }
    return {"action": "skip", "transcode": False, "delay_s": 0}


def starvation_fault_reached(
    track_events: int,
    track_total_s: float,
    *,
    max_events: int = 3,
    max_total_s: float = 20.0,
) -> bool:
    """A track crossed the cache-starvation fault threshold.

    Either repeated short starvations (measured 2026-08-08: repeat offenders
    froze up to 15x per run) or one long one (single 9-11s episodes during
    server hiccups) means the serve is bad — the caller should advance past
    the resource instead of stuttering to its natural end.
    """
    return track_events >= max_events or track_total_s >= max_total_s


def cache_starvation_recovery_plan(
    *,
    auto_transcode: bool,
    server_mode: str | None,
    already_requested: bool,
) -> dict[str, str]:
    """Choose one bounded recovery for a direct-play starvation.

    Normal playback keeps the complete library. A direct item that proves
    itself cache-starved should get one server-transcode attempt instead of
    being removed from the user's library. The explicit direct-only diagnostic
    profile and an already-transcoded item remain unchanged.
    """
    if not auto_transcode:
        return {"action": "observe", "reason": "auto_transcode_disabled"}
    if server_mode != "direct":
        return {"action": "observe", "reason": "already_transcoded"}
    if already_requested:
        return {"action": "observe", "reason": "recovery_already_requested"}
    return {"action": "transcode", "reason": "cache_starvation"}
