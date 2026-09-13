"""Display-independent Linux playback benchmark primitives.

The benchmark intentionally separates decode/transport evidence from physical
presentation evidence. It uses mpv's null video/audio outputs so a KVM that
removes HDMI/DP connectors cannot tear down the measurement process. Physical
scanout remains a separate, fail-closed production gate.
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


_DECODER_ERROR_PATTERNS = (
    re.compile(r"error while decoding", re.IGNORECASE),
    re.compile(r"error decoding frame", re.IGNORECASE),
    re.compile(r"output image buffer is null", re.IGNORECASE),
    re.compile(r"hardware accelerator failed to decode picture", re.IGNORECASE),
)
_AUDIO_ERROR_PATTERNS = (
    re.compile(r"audio device underrun", re.IGNORECASE),
    re.compile(r"audio underrun", re.IGNORECASE),
)
_TRANSPORT_ERROR_PATTERNS = (
    re.compile(r"connection refused", re.IGNORECASE),
    re.compile(r"network is unreachable", re.IGNORECASE),
    re.compile(r"http error", re.IGNORECASE),
    re.compile(r"timed out", re.IGNORECASE),
)


def read_drm_status(root: str | Path = "/sys/class/drm") -> dict[str, str]:
    """Read DRM connector status without depending on xrandr or Wayland tools."""
    base = Path(root)
    result: dict[str, str] = {}
    if not base.is_dir():
        return result
    for status_path in sorted(base.glob("*/status")):
        try:
            status = status_path.read_text(encoding="utf-8", errors="replace").strip().lower()
        except OSError:
            continue
        if status:
            result[status_path.parent.name] = status
    return result


def connector_summary(connectors: Mapping[str, str]) -> dict[str, Any]:
    """Summarize connector state, distinguishing absent telemetry from unplugged outputs."""
    known = len(connectors)
    connected = sum(value == "connected" for value in connectors.values())
    disconnected = sum(value == "disconnected" for value in connectors.values())
    if known == 0:
        status = "unknown"
    elif connected:
        status = "connected"
    elif disconnected == known:
        status = "all_disconnected"
    else:
        status = "unknown"
    return {
        "known": known,
        "connected": connected,
        "disconnected": disconnected,
        "all_disconnected": status == "all_disconnected",
        "status": status,
    }


def safe_input_label(source: str) -> str:
    """Return a stable opaque label; source URLs never enter benchmark artifacts."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("benchmark input must be a non-empty string")
    return "input-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def build_mpv_command(
    mpv_binary: str,
    source: str,
    *,
    hwdec: str,
    log_path: str | Path,
) -> list[str]:
    """Build one display-independent looping mpv cell command."""
    if not isinstance(mpv_binary, str) or not mpv_binary.strip():
        raise ValueError("mpv binary must be non-empty")
    if not isinstance(hwdec, str) or not hwdec.strip():
        raise ValueError("hwdec must be non-empty")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("benchmark input must be non-empty")
    return [
        mpv_binary,
        "--no-config",
        "--no-terminal",
        "--msg-level=all=info",
        "--vo=null",
        "--ao=null",
        f"--hwdec={hwdec}",
        "--loop-file=inf",
        "--keep-open=yes",
        f"--log-file={Path(log_path)}",
        source,
    ]


def parse_mpv_errors(text: str) -> dict[str, int]:
    """Count disqualifying decoder, audio, and transport errors by log line."""
    if not isinstance(text, str):
        raise ValueError("mpv log must be text")
    counts = {"decoder_faults": 0, "audio_underruns": 0, "transport_errors": 0}
    for line in text.splitlines():
        if any(pattern.search(line) for pattern in _DECODER_ERROR_PATTERNS):
            counts["decoder_faults"] += 1
        if any(pattern.search(line) for pattern in _AUDIO_ERROR_PATTERNS):
            counts["audio_underruns"] += 1
        if any(pattern.search(line) for pattern in _TRANSPORT_ERROR_PATTERNS):
            counts["transport_errors"] += 1
    return counts


def clock_sample() -> dict[str, float]:
    """Return suspend-aware and process-active clocks for coverage accounting."""
    monotonic = time.monotonic()
    if hasattr(time, "CLOCK_BOOTTIME"):
        boottime = time.clock_gettime(time.CLOCK_BOOTTIME)
    else:
        boottime = monotonic
    return {"monotonic": monotonic, "boottime": boottime}


def _error_total(cell_results: Sequence[Mapping[str, Any]], key: str) -> int:
    total = 0
    for cell in cell_results:
        errors = cell.get("errors", {})
        value = errors.get(key, 0) if isinstance(errors, Mapping) else 0
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            total += value
    return total


def _event_display_status(event: Mapping[str, Any]) -> str:
    summary = event.get("summary")
    if not isinstance(summary, Mapping):
        return "unknown"
    status = summary.get("status")
    if status in {"connected", "all_disconnected"}:
        return str(status)
    if summary.get("all_disconnected") is True:
        return "all_disconnected"
    return "unknown"


def _event_elapsed(event: Mapping[str, Any]) -> float | None:
    value = event.get("elapsed_monotonic_seconds")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def summarize_display_events(
    display_events: Sequence[Mapping[str, Any]],
    *,
    elapsed_monotonic: float,
) -> dict[str, Any]:
    """Classify display loss only when a connected state precedes it.

    A run that starts while the KVM is already switched away is useful for
    headless decode evidence, but it is not evidence that the application
    survived a display-loss transition. Unknown connector samples break the
    transition chain rather than being guessed through.
    """
    statuses: list[tuple[str, float | None]] = [
        (_event_display_status(event), _event_elapsed(event))
        for event in display_events
    ]
    first_known = next((status for status, _ in statuses if status != "unknown"), None)
    previous_status: str | None = None
    loss_transitions = 0
    recoveries = 0
    for status, _ in statuses:
        if status == "unknown":
            previous_status = None
            continue
        if previous_status == "connected" and status == "all_disconnected":
            loss_transitions += 1
        elif previous_status == "all_disconnected" and status == "connected":
            recoveries += 1
        previous_status = status

    loss_seconds = 0.0
    preexisting_disconnect_seconds = 0.0
    previous_status: str | None = None
    loss_active = False
    preexisting_active = False
    for index, (status, timestamp) in enumerate(statuses):
        next_timestamp = elapsed_monotonic
        for _next_status, candidate_timestamp in statuses[index + 1:]:
            if candidate_timestamp is not None:
                next_timestamp = candidate_timestamp
                break
        duration = 0.0
        if timestamp is not None:
            duration = max(0.0, next_timestamp - timestamp)
        if status == "unknown":
            loss_active = False
            preexisting_active = False
            previous_status = None
            continue
        if previous_status == "connected" and status == "all_disconnected":
            loss_active = True
        elif previous_status == "all_disconnected" and status == "connected":
            loss_active = False
            preexisting_active = False
        elif previous_status is None and status == "all_disconnected":
            preexisting_active = True
        if status == "all_disconnected":
            if loss_active:
                loss_seconds += duration
            elif preexisting_active:
                preexisting_disconnect_seconds += duration
        previous_status = status

    disconnect_samples = sum(status == "all_disconnected" for status, _ in statuses)
    return {
        "display_preexisting_disconnect": first_known == "all_disconnected",
        "display_disconnect_samples": disconnect_samples,
        "display_loss_transitions": loss_transitions,
        "display_recoveries": recoveries,
        "display_loss_seconds": round(loss_seconds, 3),
        "display_preexisting_disconnect_seconds": round(preexisting_disconnect_seconds, 3),
        # Compatibility field: this now means observed loss transitions, not
        # merely repeated samples of a pre-existing disconnected state.
        "display_disconnects": loss_transitions,
    }


def summarize_run(
    *,
    requested_seconds: int | float,
    elapsed_monotonic: int | float,
    elapsed_boottime: int | float,
    cell_results: Sequence[Mapping[str, Any]],
    display_events: Sequence[Mapping[str, Any]],
    expected_cells: int = 8,
) -> dict[str, Any]:
    """Build the fail-closed headless and physical-display benchmark summary."""
    if requested_seconds <= 0 or elapsed_monotonic < 0 or elapsed_boottime < 0:
        raise ValueError("benchmark clock values must be non-negative and duration positive")
    if expected_cells < 1:
        raise ValueError("expected_cells must be positive")
    suspend_gap = max(0.0, float(elapsed_boottime) - float(elapsed_monotonic))
    active_coverage = min(1.0, float(elapsed_monotonic) / float(requested_seconds))
    early_exits = sum(bool(cell.get("early_exit")) for cell in cell_results)
    wrong_returncodes = sum(
        cell.get("returncode") not in (0, None) and not cell.get("normal_stop", False)
        for cell in cell_results
    )
    error_counts = {
        key: _error_total(cell_results, key)
        for key in ("decoder_faults", "audio_underruns", "transport_errors")
    }
    decode_transport_verdict = (
        "PASS"
        if len(cell_results) == expected_cells
        and early_exits == 0
        and wrong_returncodes == 0
        and active_coverage >= 0.95
        and suspend_gap < max(2.0, float(requested_seconds) * 0.05)
        and not any(error_counts.values())
        else "BLOCK"
    )
    display_metrics = summarize_display_events(
        display_events,
        elapsed_monotonic=float(elapsed_monotonic),
    )
    disconnects = display_metrics["display_loss_transitions"]
    if disconnects == 0:
        resilience = "WARNING"
    elif decode_transport_verdict == "PASS":
        resilience = "PASS"
    else:
        resilience = "BLOCK"
    return {
        "verdict": "BLOCK",  # physical presentation is intentionally unmeasured here
        "benchmark_mode": "linux_headless_decode_transport",
        "presentation_gate": "BLOCK",
        "presentation_quality": "unmeasured_without_physical_scanout",
        "decode_transport_verdict": decode_transport_verdict,
        "headless_disconnect_resilience": resilience,
        "requested_seconds": float(requested_seconds),
        "elapsed_monotonic_seconds": round(float(elapsed_monotonic), 3),
        "elapsed_boottime_seconds": round(float(elapsed_boottime), 3),
        "suspend_gap_seconds": round(suspend_gap, 3),
        "active_coverage": round(active_coverage, 4),
        "expected_cells": expected_cells,
        "observed_cells": len(cell_results),
        "early_exits": early_exits,
        "wrong_returncodes": wrong_returncodes,
        **display_metrics,
        "error_counts": error_counts,
    }
