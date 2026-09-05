"""Fail-closed capacity selection for measured macOS cell profiles."""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


M5_CELL_MODES = (4, 6, 8)
M5_MIN_DURATION_COVERAGE = 0.95
M5_MAX_P95_LOOP_LAG_MS = 25.0
M5_MAX_RENDER_GAP_MS = 100.0
_REQUIRED_METRICS = (
    "duration_coverage",
    "p95_loop_lag_ms",
    "max_render_gap_ms",
    "cpu_cores_mean",
    "loop_stalls_ge_100ms",
    "freeze_count",
    "decoder_faults",
    "audio_underruns",
    "av_desync",
    "transport_errors",
    "power_sleep_evidence",
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _sum_metrics(source: Mapping[str, Any], keys: tuple[str, ...]) -> int | float | None:
    values = [_number(source.get(key)) for key in keys]
    if any(value is None for value in values):
        return None
    numbers = [value for value in values if value is not None]
    total = sum(numbers)
    return int(total) if all(float(value).is_integer() for value in numbers) else total


def capacity_profile_from_analysis(
    analysis: Mapping[str, Any],
    *,
    native_profile: Mapping[str, Any] | None = None,
    cell_count: int | None = None,
) -> dict[str, Any]:
    """Project ``diagnostics.analyze_run`` output into capacity metrics.

    The projection is intentionally conservative. It returns ``None`` for any
    missing source field so ``select_capacity`` can fail closed rather than
    interpreting unavailable evidence as a zero-error run.
    """
    stats = analysis.get("stats")
    stats = stats if isinstance(stats, Mapping) else {}
    log = analysis.get("log")
    log = log if isinstance(log, Mapping) else {}
    gates = analysis.get("gates")
    gates = gates if isinstance(gates, Mapping) else {}
    coverage_gate = gates.get("duration_coverage")
    coverage_value = coverage_gate.get("value") if isinstance(coverage_gate, Mapping) else None
    duration_coverage = (
        coverage_value.get("coverage")
        if isinstance(coverage_value, Mapping)
        else None
    )
    if cell_count is None:
        observed_cells = stats.get("n_cells")
        cell_count = observed_cells if isinstance(observed_cells, int) and not isinstance(observed_cells, bool) else None
    render_rows = stats.get("render_telemetry")
    render_gaps = []
    if isinstance(render_rows, list):
        for row in render_rows:
            if isinstance(row, Mapping):
                value = _number(row.get("paint_gap_max_ms"))
                if value is not None:
                    render_gaps.append(value)
    cpu_cores_mean = None
    if isinstance(native_profile, Mapping):
        power = native_profile.get("powermetrics")
        process = power.get("process") if isinstance(power, Mapping) else None
        cpu = process.get("cpu_ms_s") if isinstance(process, Mapping) else None
        cpu_cores_mean = cpu.get("mean") if isinstance(cpu, Mapping) else None
    if cpu_cores_mean is None:
        cpu_cores_mean = analysis.get("cpu_cores_mean")
    decoder_faults = _sum_metrics(
        log,
        (
            "hardware_decode_failures",
            "decoder_buffer_warnings",
            "video_decode_errors",
            "audio_decode_errors",
        ),
    )
    transport_errors = _sum_metrics(
        log,
        (
            "connection_refused",
            "hls_segment_failures",
            "stream_open_failures",
            "playback_errors",
            "retry_skips",
        ),
    )
    power_gate = gates.get("power_sleep_evidence")
    power_sleep_evidence = (
        1
        if isinstance(power_gate, Mapping) and power_gate.get("status") == "PASS"
        else None
    )
    return {
        "cell_count": cell_count,
        "duration_coverage": _number(duration_coverage),
        "p95_loop_lag_ms": _number(log.get("p95_loop_lag_ms")),
        "max_render_gap_ms": max(render_gaps) if render_gaps else None,
        "cpu_cores_mean": _number(cpu_cores_mean),
        "loop_stalls_ge_100ms": _number(log.get("loop_stalls_ge_100ms")),
        "freeze_count": _number(log.get("freeze_count")),
        "decoder_faults": decoder_faults,
        "audio_underruns": _number(log.get("audio_underrun")),
        "av_desync": _number(log.get("av_desync")),
        "transport_errors": transport_errors,
        "power_sleep_evidence": power_sleep_evidence,
    }


def _candidate(profile: Mapping[str, Any]) -> dict[str, Any]:
    cell_count = profile.get("cell_count")
    result: dict[str, Any] = {
        "cell_count": cell_count,
        "status": "BLOCK",
        "missing": [],
        "failures": [],
    }
    if (
        isinstance(cell_count, bool)
        or not isinstance(cell_count, int)
        or cell_count not in M5_CELL_MODES
    ):
        result["missing"] = ["supported_cell_count"]
        return result
    missing: list[str] = []
    failures: list[str] = []
    for metric in _REQUIRED_METRICS:
        value = _number(profile.get(metric))
        if value is None:
            missing.append(metric)
        elif metric == "duration_coverage":
            if value < M5_MIN_DURATION_COVERAGE:
                failures.append(metric)
        elif metric == "p95_loop_lag_ms":
            if value > M5_MAX_P95_LOOP_LAG_MS:
                failures.append(metric)
        elif metric == "max_render_gap_ms":
            if value > M5_MAX_RENDER_GAP_MS:
                failures.append(metric)
        elif metric == "power_sleep_evidence":
            if value != 1:
                failures.append(metric)
        elif metric != "cpu_cores_mean" and value != 0:
            failures.append(metric)
    result["missing"] = missing
    result["failures"] = failures
    if not missing and not failures:
        result["status"] = "PASS"
    return result


def select_capacity(
    profiles: Iterable[Mapping[str, Any]],
    *,
    allowed_cells: tuple[int, ...] = M5_CELL_MODES,
) -> dict[str, Any]:
    """Select the highest passing measured mode, or fail closed.

    A candidate with a valid supported cell count but a failed metric is a
    legitimate rejected measurement and does not prevent selecting a lower
    passing mode. Invalid/duplicate cell counts are input-integrity failures
    and block the whole decision.
    """
    allowed = tuple(sorted(set(allowed_cells)))
    candidates: list[dict[str, Any]] = []
    invalid_profiles: list[Any] = []
    seen: set[int] = set()
    duplicate_cells: set[int] = set()
    for profile in profiles:
        if not isinstance(profile, Mapping):
            invalid_profiles.append(None)
            continue
        cell_count = profile.get("cell_count")
        if (
            isinstance(cell_count, bool)
            or not isinstance(cell_count, int)
            or cell_count not in allowed
        ):
            invalid_profiles.append(cell_count)
            continue
        if cell_count in seen:
            duplicate_cells.add(cell_count)
        seen.add(cell_count)
        candidates.append(_candidate(profile))
    candidates.sort(key=lambda item: item["cell_count"])
    passing = [item for item in candidates if item["status"] == "PASS"]
    selected = max((item["cell_count"] for item in passing), default=None)
    first_failing = [
        item["cell_count"] for item in candidates if item["status"] == "BLOCK"
    ]
    integrity_block = bool(invalid_profiles or duplicate_cells)
    return {
        "status": "PASS" if selected is not None and not integrity_block else "BLOCK",
        "selected_cells": selected if not integrity_block else None,
        "supported_cells": list(allowed),
        "first_failing_cells": first_failing,
        "invalid_profiles": sorted(invalid_profiles, key=lambda value: (value is None, value)),
        "duplicate_cells": sorted(duplicate_cells),
        "candidates": candidates,
    }


__all__ = [
    "M5_CELL_MODES",
    "M5_MAX_P95_LOOP_LAG_MS",
    "M5_MAX_RENDER_GAP_MS",
    "M5_MIN_DURATION_COVERAGE",
    "select_capacity",
]
