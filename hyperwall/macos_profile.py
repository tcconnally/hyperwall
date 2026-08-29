"""Sanitized parsers for bounded macOS Hyperwall performance profiles."""
from __future__ import annotations

from collections import Counter
import math
import re
from statistics import mean, median
from typing import Any, Iterable


_SAMPLE_RE = re.compile(r"^\*\*\* Sampled system activity \(")
_TASK_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<pid>-?\d+)\s+"
    r"(?P<cpu>[0-9]+(?:\.[0-9]+)?)\s+"
    r"(?P<user>[0-9]+(?:\.[0-9]+)?)\s+"
)
_THERMAL_RE = re.compile(r"^Current pressure level:\s*(?P<level>\S.*)\s*$")
_GPU_IDLE_RE = re.compile(r"^GPU idle residency:\s*(?P<idle>[0-9]+(?:\.[0-9]+)?)%")
_THREAD_RE = re.compile(r"^\s*Thread\b")
_PERMISSION_MARKERS = (
    "must be invoked as the superuser",
    "not permitted",
    "permission denied",
)
_NATIVE_LABELS = (
    "mpv_render_context_render",
    "avcodec_send_packet",
    "QOpenGLWidget::paintGL",
    "_PyEval_EvalFrameDefault",
    "_CallPythonObject",
    "libmpv",
    "libavcodec",
    "WindowServer",
)


def _summary(values: Iterable[float]) -> dict[str, float] | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return {
        "min": ordered[0],
        "mean": mean(ordered),
        "median": median(ordered),
        "p95": ordered[p95_index],
        "max": ordered[-1],
    }


def _task_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _TASK_RE.match(line.strip())
        if not match:
            continue
        rows.append({
            "name": match.group("name").strip(),
            "pid": int(match.group("pid")),
            "cpu_ms_s": float(match.group("cpu")),
            "user_pct": float(match.group("user")),
        })
    return rows


def parse_powermetrics(
    text: str,
    *,
    process_name: str = "Python",
    process_pid: int | None = None,
) -> dict[str, Any]:
    """Parse task, thermal, and GPU fields from text-mode powermetrics."""
    source = str(text or "")
    lines = source.splitlines()
    sample_count = sum(1 for line in lines if _SAMPLE_RE.search(line))
    rows = _task_rows(source)
    target_rows = [
        row for row in rows
        if row["name"] == process_name
        and (process_pid is None or row["pid"] == process_pid)
    ]
    thermal = Counter(
        match.group("level").strip()
        for line in lines
        if (match := _THERMAL_RE.match(line))
    )
    gpu_idle = [
        float(match.group("idle"))
        for line in lines
        if (match := _GPU_IDLE_RE.match(line))
    ]
    missing: list[str] = []
    lowered = source.lower()
    if not source.strip():
        missing.append("powermetrics_missing")
    if any(marker in lowered for marker in _PERMISSION_MARKERS):
        missing.append("permission_denied")
    if sample_count == 0:
        missing.append("activity_samples_missing")
    if not target_rows:
        missing.append("target_process_missing")
    process: dict[str, Any] = {
        "name": process_name,
        "pid": process_pid,
        "samples": len(target_rows),
        "cpu_ms_s": _summary(row["cpu_ms_s"] for row in target_rows),
        "user_pct": _summary(row["user_pct"] for row in target_rows),
    }
    if process_pid is None and target_rows:
        pids = sorted({row["pid"] for row in target_rows})
        process["pids"] = pids
    return {
        "complete": not missing,
        "missing_evidence": sorted(set(missing)),
        "sample_count": sample_count,
        "process": process,
        "all_tasks": {
            "samples": sum(1 for row in rows if row["name"] == "ALL_TASKS"),
            "cpu_ms_s": _summary(row["cpu_ms_s"] for row in rows if row["name"] == "ALL_TASKS"),
        },
        "thermal_levels": dict(sorted(thermal.items())),
        "gpu_idle_residency": _summary(gpu_idle),
    }


def parse_sample_stacks(text: str) -> dict[str, Any]:
    """Parse bounded native stack labels from macOS ``sample`` output."""
    source = str(text or "")
    thread_count = sum(1 for line in source.splitlines() if _THREAD_RE.match(line))
    labels = Counter()
    for line in source.splitlines():
        for label in _NATIVE_LABELS:
            if label in line:
                labels[label] += 1
    missing: list[str] = []
    if not source.strip():
        missing.append("sample_missing")
    if thread_count == 0:
        missing.append("sample_threads_missing")
    return {
        "complete": not missing,
        "missing_evidence": sorted(set(missing)),
        "thread_count": thread_count,
        "labels": dict(sorted(labels.items())),
    }


__all__ = ["parse_powermetrics", "parse_sample_stacks"]
