"""Offline analysis and redaction helpers for Hyperwall soak artifacts.

The live runner intentionally collects no screenshots or screen recordings.
This module stays dependency-free so it can parse results on macOS, Linux, or
CI without importing Qt, mpv, or making network requests.
"""
from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

_AUTH_SCHEME = "".join(chr(value) for value in (66, 101, 97, 114, 101, 114))

_REDACTIONS = (
    (
        re.compile(
            r'(?i)("(?:api_key|apikey|playsessionid|authorization|x-emby-token|'
            r'access_token|accesstoken|password|token|secret)"\s*:\s*)'
            r'("[^"\r\n]*"|[^,}\s]+)'
        ),
        r'\1"<redacted>"',
    ),
    (re.compile(r"(?i)(FREEZE:\s*)[0-9.]+s"), r"\1<redacted>"),
    (re.compile(r"(?i)(PERF loop stall:.*?~)[0-9.]+ms"), r"\1<redacted>ms"),
    (re.compile(r"(?i)(PERF slow slot .*?:\s*)[0-9.]+ms"), r"\1<redacted>ms"),
    (re.compile(r"(?i)(api_key\s*[=:]\s*)[^&\s,}\"']+"), r"\1<redacted>"),
    (re.compile(r"(?i)(PlaySessionId\s*[=:]\s*)[^&\s,}\"']+"), r"\1<redacted>"),
    (
        re.compile(
            r"(?i)(Authorization\s*[:=]\s*)(?:(?:Bearer|Basic)[ \t]+)?"
            r"[^ \t\r\n,;}]+"
        ),
        r"\1<redacted>",
    ),
    (re.compile(r"(?i)(Serial Number(?: \(system\))?:\s*)[^\r\n]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(Hardware UUID:\s*)[^\r\n]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(Provisioning UDID:\s*)[^\r\n]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(Hardware UUID|Serial Number|Provisioning UDID)=[^\s]+"), r"\1=<redacted>"),
    (re.compile(r"(?i)(/(?:Users|home|opt/data)/)[^/\s]+"), r"\1<user>"),
    (re.compile(r"(?i)(X-Emby-Token\s*[:=]\s*)[^\r\n]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(AccessToken\s*[=:]\s*)[^&\s,}\"']+"), r"\1<redacted>"),
    (re.compile(r"(?i)(password\s*[=:]\s*)[^&\s,}\"']+"), r"\1<redacted>"),
    (re.compile(r"(?i)(token\s*[=:]\s*)[^&\s,}\"']+"), r"\1<redacted>"),
    (re.compile(r"(?i)(secret\s*[=:]\s*)[^&\s,}\"']+"), r"\1<redacted>"),
)

_LOG_PATTERNS = {
    "connection_refused": re.compile(r"Connection refused"),
    "hls_segment_failures": re.compile(r"Failed to open segment|failed too many times"),
    "stream_open_failures": re.compile(r"Failed to open https?://"),
    "hardware_decode_failures": re.compile(
        r"hardware accelerator failed to decode picture|Error while decoding frame \(hardware decoding\)"
    ),
    "decoder_buffer_warnings": re.compile(r"output image buffer is null"),
    "video_decode_errors": re.compile(r"ERROR mpv\[ffmpeg/video\]"),
    "audio_decode_errors": re.compile(r"ERROR mpv\[(?:ad|ffmpeg/audio)\]"),
    "av_desync": re.compile(r"Audio/Video desynchronisation detected"),
    "audio_underrun": re.compile(r"Audio device underrun detected"),
    "loop_stalls": re.compile(r"PERF loop stall: main thread blocked ~([^\s]+)\s*ms"),
    "slow_slots": re.compile(r"PERF slow slot [^:]+:\s*([^\s]+)\s*ms"),
    "playback_errors": re.compile(r"Playback error"),
    "retry_skips": re.compile(r"Max retries reached"),
    "crash_loop_guard": re.compile(r"Crash-loop guard"),
}
_LOOP_LAG_SUMMARY_RE = re.compile(
    r"PERF loop-lag ms:.*?p95\s+(?P<p95>[^\s]+)"
)
_PLAYBACK_PLAN_RE = re.compile(
    r"Playback plan: (DIRECT|TRANSCODE)(/prefetch)?"
)
_FREEZE_RE = re.compile(r"FREEZE:\s*([^\s]+)\s*s")
_FREEZE_MARKER_RE = re.compile(r"FREEZE:", re.IGNORECASE)
_STATS_MARKER_RE = re.compile(r"\bSTATS\s+cell\b", re.IGNORECASE)
_UNKNOWN_NUMERIC = {"", "?", "none", "unknown", "n/a", "na"}
_STATS_RE = re.compile(
    r"STATS cell (\d+)\s+drop=([\d.]+).*?freezes=(\d+)\(([\d.]+)s\).*?"
    r"hwdec=([^\s]+)\s+fps=([^\s]+)\s+bitrate=([^\s]+)"
)


def redact_text(text: str) -> str:
    """Redact credentials and host identifiers before an artifact is shared."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


_SENSITIVE_JSON_KEY_RE = re.compile(
    r'(?i)"[^"\r\n]*(?:api[_-]?key|authorization|access[_-]?token|'
    r'playsessionid|x-emby-token|password|token|secret)[^"\r\n]*"\s*:'
)


def _contains_sensitive_fragment(text: str) -> bool:
    return bool(
        _SENSITIVE_JSON_KEY_RE.search(text)
        or re.search(
            r"(?i)(api_key|apikey|playsessionid|authorization|x-emby-token|"
            r"access_token|accesstoken|password|token|secret)\s*(?:[:=]|%3[dD])",
            text,
        )
    )


def _is_trusted_macos_var_alias(path: Path) -> bool:
    """Allow only the fixed macOS /var -> /private/var system alias."""
    if sys.platform != "darwin" or path != Path("/var"):
        return False
    return os.path.realpath(os.fspath(path)) == "/private/var"


def _reject_symlink_components(path: str | Path) -> Path:
    """Reject symlinked path components before any read or write."""
    absolute = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                if _is_trusted_macos_var_alias(current):
                    continue
                raise ValueError(f"path contains symlink component: {current}")
        except FileNotFoundError:
            continue
    return absolute


def _reject_existing_symlink_children(root: Path) -> None:
    try:
        children = list(root.iterdir())
    except OSError as exc:
        raise ValueError(f"cannot inspect path components under {root}") from exc
    for child in children:
        if child.is_symlink():
            raise ValueError(f"path contains symlink child: {child}")


def force_private_permissions(path: str | Path, mode: int) -> None:
    """Enforce owner-only diagnostic permissions on every supported OS."""
    # Preserve an already-constructed Path; this also keeps callers that
    # pass a POSIX Path stable when platform detection is mocked in tests.
    target = path if isinstance(path, Path) else Path(path)
    if os.name == "nt":
        # chmod is still a useful writable-bit check on Windows; the ACL is
        # the privacy boundary, because Windows does not model 0700/0600 as
        # POSIX permission bits.
        target.chmod(mode)
        principal = os.environ.get("USERNAME", "").strip()
        if not principal:
            raise PermissionError("cannot identify the Windows diagnostic owner")
        result = subprocess.run(
            [
                "icacls",
                os.fspath(target),
                "/inheritance:r",
                "/grant:r",
                f"{principal}:F",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            raise PermissionError(f"private ACL enforcement failed for {target}")
        return
    target.chmod(mode)
    actual = target.stat().st_mode & 0o777
    if actual != mode:
        raise PermissionError(f"private mode enforcement failed for {target}")


def redact_json_value(value: Any) -> Any:
    """Recursively redact strings in structured telemetry."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_json_value(item) for item in value]
    if isinstance(value, dict):
        def key_is_sensitive(key: object) -> bool:
            normalized = str(key).casefold().replace("-", "_")
            return bool(re.search(
                r"(?:api[_-]?key|authorization|access[_-]?token|playsessionid|"
                r"x_emby_token|password|token|secret)",
                normalized,
            ))

        return {
            str(key): "<redacted>" if key_is_sensitive(key) else redact_json_value(item)
            for key, item in value.items()
        }
    return value


def _max_or_zero(values: list[float]) -> float:
    return max(values) if values else 0.0


def _finite_number(value: Any, *, nonnegative: bool = False) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(float(value)):
        return False
    return not nonnegative or value >= 0


def parse_app_log(text: str) -> dict[str, Any]:
    """Extract reliability counters from a Hyperwall application log."""
    counts: dict[str, Any] = {key: 0 for key in _LOG_PATTERNS}
    counts["freeze_count"] = 0
    freeze_seconds = 0.0
    max_loop_stall: list[float] = []
    active_loop_stall: list[float] = []
    shutdown_loop_stall: list[float] = []
    p95_loop_lag: list[float] = []
    loop_stalls_ge_100ms = 0
    max_slow_slot: list[float] = []
    shutdown_started = False
    stats: list[dict[str, Any]] = []
    playback_plan_counts = {
        "direct": 0,
        "direct_prefetch": 0,
        "server_transcode": 0,
        "server_transcode_prefetch": 0,
    }
    malformed_numeric = 0
    for line in text.splitlines():
        if re.search(r"Shutdown requested\.", line):
            shutdown_started = True
        lag_summary = _LOOP_LAG_SUMMARY_RE.search(line)
        if lag_summary and not shutdown_started:
            try:
                value = float(lag_summary.group("p95"))
                if not math.isfinite(value) or value < 0:
                    malformed_numeric += 1
                else:
                    p95_loop_lag.append(value)
            except (TypeError, ValueError, OverflowError):
                malformed_numeric += 1
        for key, pattern in _LOG_PATTERNS.items():
            match = pattern.search(line)
            if match:
                counts[key] += 1
                if key == "loop_stalls" or key == "slow_slots":
                    try:
                        value = float(match.group(1))
                        if not math.isfinite(value) or value < 0:
                            malformed_numeric += 1
                        elif key == "loop_stalls":
                            max_loop_stall.append(value)
                            if shutdown_started:
                                shutdown_loop_stall.append(value)
                            else:
                                active_loop_stall.append(value)
                                if value >= 100.0:
                                    loop_stalls_ge_100ms += 1
                        else:
                            max_slow_slot.append(value)
                    except (TypeError, ValueError, OverflowError):
                        malformed_numeric += 1
        plan_match = _PLAYBACK_PLAN_RE.search(line)
        if plan_match:
            plan_key = (
                "server_transcode"
                if plan_match.group(1) == "TRANSCODE"
                else "direct"
            )
            if plan_match.group(2):
                plan_key += "_prefetch"
            playback_plan_counts[plan_key] += 1
        freeze = _FREEZE_RE.search(line)
        if _FREEZE_MARKER_RE.search(line):
            if not freeze:
                malformed_numeric += 1
            else:
                try:
                    seconds = float(freeze.group(1))
                    if not math.isfinite(seconds) or seconds < 0:
                        malformed_numeric += 1
                    else:
                        counts["freeze_count"] = counts.get("freeze_count", 0) + 1
                        freeze_seconds += seconds
                except (TypeError, ValueError, OverflowError):
                    malformed_numeric += 1
        stat = _STATS_RE.search(line)
        if _STATS_MARKER_RE.search(line):
            if not stat:
                malformed_numeric += 1
            else:
                cell, drops, freezes, seconds, hwdec, fps, bitrate = stat.groups()
                try:
                    row = {
                        "cell": int(cell),
                        "drop": float(drops),
                        "freezes": int(freezes),
                        "freeze_seconds": float(seconds),
                        "hwdec": hwdec,
                        "fps": fps,
                        "bitrate": bitrate,
                    }
                    if all(
                        math.isfinite(float(row[key])) and float(row[key]) >= 0
                        for key in ("drop", "freezes", "freeze_seconds")
                    ):
                        stats.append(row)
                    else:
                        malformed_numeric += 1
                except (TypeError, ValueError, OverflowError):
                    malformed_numeric += 1
    counts["freeze_seconds"] = round(freeze_seconds, 1)
    counts["max_loop_stall_ms"] = _max_or_zero(active_loop_stall)
    counts["max_loop_stall_ms_including_shutdown"] = _max_or_zero(max_loop_stall)
    counts["loop_stalls_ge_100ms"] = loop_stalls_ge_100ms
    counts["p95_loop_lag_ms"] = max(p95_loop_lag) if p95_loop_lag else None
    counts["shutdown_loop_stalls"] = len(shutdown_loop_stall)
    counts["max_shutdown_loop_stall_ms"] = _max_or_zero(shutdown_loop_stall)
    counts["max_slow_slot_ms"] = _max_or_zero(max_slow_slot)
    counts["stats"] = stats
    counts["playback_plan_counts"] = playback_plan_counts
    counts["malformed_numeric_fields"] = malformed_numeric
    return counts


def parse_soak_jsonl(text: str) -> dict[str, Any]:
    """Parse the structured start/sample/finish soak manifest."""
    records: list[dict[str, Any]] = []
    malformed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            malformed += 1
    starts = [record for record in records if record.get("event") == "start"]
    samples = [record for record in records if record.get("event") == "sample"]
    finishes = [record for record in records if record.get("event") == "finish"]
    start = starts[0] if starts else {}
    finish = finishes[-1] if finishes else {}
    baseline_value = start.get("baseline") if start else None
    resources_value = finish.get("resources") if finish else None
    baseline = baseline_value if isinstance(baseline_value, dict) else {}
    resources = resources_value if isinstance(resources_value, dict) else {}
    baseline_ws: Any = baseline.get("ws_mb")
    final_ws: Any = resources.get("ws_mb")
    baseline_current_ws: Any = baseline.get("current_ws_mb")
    final_current_ws: Any = resources.get("current_ws_mb")
    duration = finish.get("wall_seconds") if finish else None
    if duration is None and samples:
        duration = samples[-1].get("wall_seconds")
    if not _finite_number(duration, nonnegative=True):
        duration = None
    invariant_value = finish.get("invariant_violations") if finish else None
    invariant = invariant_value
    metric_values = [
        value
        for value in (baseline.get("ws_metric"), resources.get("ws_metric"))
        if isinstance(value, str) and value
    ]
    ws_metric = None
    if metric_values:
        ws_metric = metric_values[0] if len(set(metric_values)) == 1 else "mixed"
    current_metric_values = [
        value
        for value in (baseline.get("current_ws_metric"), resources.get("current_ws_metric"))
        if isinstance(value, str) and value
    ]
    current_ws_metric = None
    if current_metric_values:
        current_ws_metric = (
            current_metric_values[0]
            if len(set(current_metric_values)) == 1
            else "mixed"
        )
    result: dict[str, Any] = {
        "record_count": len(records),
        "malformed_records": malformed,
        "sample_count": len(samples),
        "samples": samples,
        "baseline_ws_mb": baseline_ws,
        "final_ws_mb": final_ws,
        "ws_metric": ws_metric,
        "baseline_current_ws_mb": baseline_current_ws,
        "final_current_ws_mb": final_current_ws,
        "current_ws_metric": current_ws_metric,
        "duration_seconds": duration,
        "invariant_violations": invariant,
        "finish_event_present": bool(finish),
        "start_event_present": bool(start),
        "start_event_count": len(starts),
        "finish_event_count": len(finishes),
    }
    if (
        _finite_number(baseline_ws, nonnegative=True)
        and _finite_number(final_ws, nonnegative=True)
    ):
        result["working_set_growth_mb"] = float(final_ws) - float(baseline_ws)
    else:
        result["working_set_growth_mb"] = None
    if (
        _finite_number(baseline_current_ws, nonnegative=True)
        and _finite_number(final_current_ws, nonnegative=True)
    ):
        result["current_working_set_growth_mb"] = (
            float(final_current_ws) - float(baseline_current_ws)
        )
    else:
        result["current_working_set_growth_mb"] = None
    return result


def _find_file(root: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = root / name
        if path.is_file() and not path.is_symlink():
            return path
    return None


def _safe_children(root: Path, pattern: str) -> list[Path]:
    """Return only regular, non-symlink direct children matching pattern."""
    result: list[Path] = []
    for candidate in root.glob(pattern):
        try:
            if candidate.parent == root and not candidate.is_symlink() and candidate.is_file():
                result.append(candidate)
        except OSError:
            continue
    return result


def _valid_resource_map(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    metric = value.get("ws_metric")
    if metric is not None and metric not in {"peak_rss_mb", "working_set_mb"}:
        return False
    current_metric = value.get("current_ws_metric")
    if current_metric is not None and current_metric not in {
        "resident_rss_mb",
        "working_set_mb",
    }:
        return False
    for key in ("ws_mb", "current_ws_mb", "private_mb", "threads"):
        if key in value and not _finite_number(value[key], nonnegative=True):
            return False
    return "ws_mb" in value and _finite_number(value["ws_mb"], nonnegative=True)


def _valid_sample_record(sample: Any) -> bool:
    if not isinstance(sample, dict):
        return False
    if not _finite_number(sample.get("wall_seconds"), nonnegative=True):
        return False
    resources = sample.get("resources")
    if not _valid_resource_map(resources):
        return False
    for key in ("actions", "invariant_violations"):
        if key in sample and key == "invariant_violations":
            value = sample[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
                or value != int(value)
            ):
                return False
    return True


def _valid_event_shape(manifest: dict[str, Any]) -> bool:
    return (
        isinstance(manifest.get("baseline"), dict)
        and _valid_resource_map(manifest["baseline"])
        and isinstance(manifest.get("resources"), dict)
        and _valid_resource_map(manifest["resources"])
        and _finite_number(manifest.get("duration_seconds"), nonnegative=True)
        and isinstance(manifest.get("sample_count"), int)
        and manifest["sample_count"] >= 1
    )


def _stats_summary(stats_path: Path | None) -> dict[str, Any]:
    """Return credential-free metadata from the final stats artifact."""
    if stats_path is None:
        return {"n_cells": None, "final_server_modes": {}}
    try:
        value = json.loads(stats_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"n_cells": None, "final_server_modes": {}}
    if not isinstance(value, dict):
        return {"n_cells": None, "final_server_modes": {}}
    cells = value.get("cells")
    summary: dict[str, Any] = {
        "n_cells": len(cells) if isinstance(cells, list) else None,
        "final_server_modes": {},
    }
    reported_cells = value.get("n_cells")
    if isinstance(reported_cells, int) and not isinstance(reported_cells, bool):
        summary["reported_n_cells"] = reported_cells
    modes: dict[str, int] = {}
    if isinstance(cells, list):
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            plan = cell.get("playback_plan")
            mode = plan.get("server_mode") if isinstance(plan, dict) else None
            if mode in {"direct", "server_transcode"}:
                modes[mode] = modes.get(mode, 0) + 1
    summary["final_server_modes"] = modes
    render_fields = (
        "frame_ready",
        "paint_calls",
        "render_calls",
        "render_errors",
        "paint_total_ms",
        "paint_max_ms",
        "render_total_ms",
        "render_max_ms",
        "paint_gap_max_ms",
        "paint_gap_last_ms",
    )
    render_rows: list[dict[str, Any]] = []
    frame_pump_rows: list[dict[str, Any]] = []
    decoder_rows: list[dict[str, Any]] = []
    audio_rows: list[dict[str, Any]] = []
    frame_pump_numeric_fields = (
        "callbacks",
        "queued_updates",
        "coalesced_callbacks",
        "ignored_callbacks",
    )
    frame_pump_boolean_fields = ("pending", "closed")
    decoder_string_fields = ("requested", "active")
    decoder_numeric_fields = (
        "fault_count",
        "hardware_attempts",
        "hardware_successes",
        "software_fallbacks",
        "recovery_exhausted",
        "quarantines",
    )
    decoder_boolean_fields = ("software_fallback", "resource_quarantined")
    if isinstance(cells, list):
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            cell_id = cell.get("cell")
            if isinstance(cell_id, bool) or not isinstance(cell_id, int):
                continue
            render_value = cell.get("render_telemetry")
            if isinstance(render_value, dict):
                safe_render: dict[str, Any] = {"cell": cell_id}
                for key in render_fields:
                    metric = render_value.get(key)
                    if (
                        isinstance(metric, (int, float))
                        and not isinstance(metric, bool)
                        and math.isfinite(float(metric))
                        and metric >= 0
                    ):
                        safe_render[key] = metric
                if len(safe_render) > 1:
                    render_rows.append(safe_render)
            frame_pump_value = cell.get("frame_pump")
            if isinstance(frame_pump_value, dict):
                safe_frame_pump: dict[str, Any] = {"cell": cell_id}
                for key in frame_pump_numeric_fields:
                    metric = frame_pump_value.get(key)
                    if (
                        isinstance(metric, (int, float))
                        and not isinstance(metric, bool)
                        and math.isfinite(float(metric))
                        and metric >= 0
                    ):
                        safe_frame_pump[key] = metric
                for key in frame_pump_boolean_fields:
                    metric = frame_pump_value.get(key)
                    if isinstance(metric, bool):
                        safe_frame_pump[key] = metric
                if len(safe_frame_pump) > 1:
                    frame_pump_rows.append(safe_frame_pump)
            decoder_value = cell.get("decoder")
            if isinstance(decoder_value, dict):
                safe_decoder: dict[str, Any] = {"cell": cell_id}
                for key in decoder_string_fields:
                    decoder_field = decoder_value.get(key)
                    if isinstance(decoder_field, str) and decoder_field:
                        safe_decoder[key] = decoder_field
                for key in decoder_numeric_fields:
                    metric = decoder_value.get(key)
                    if (
                        isinstance(metric, (int, float))
                        and not isinstance(metric, bool)
                        and math.isfinite(float(metric))
                        and metric >= 0
                    ):
                        safe_decoder[key] = metric
                for key in decoder_boolean_fields:
                    decoder_flag = decoder_value.get(key)
                    if isinstance(decoder_flag, bool):
                        safe_decoder[key] = decoder_flag
                if len(safe_decoder) > 1:
                    decoder_rows.append(safe_decoder)
            audio_value = cell.get("audio_state")
            if isinstance(audio_value, dict):
                muted = audio_value.get("muted")
                audio_started = audio_value.get("audio_started")
                if isinstance(muted, bool) and isinstance(audio_started, bool):
                    audio_rows.append({
                        "cell": cell_id,
                        "muted": muted,
                        "audio_started": audio_started,
                    })
    if render_rows:
        summary["render_telemetry"] = render_rows
    if frame_pump_rows:
        summary["frame_pump"] = frame_pump_rows
    if decoder_rows:
        summary["decoder"] = decoder_rows
    if audio_rows:
        summary["audio_state"] = audio_rows
    policy = value.get("playback_policy")
    if isinstance(policy, dict):
        safe_policy: dict[str, object] = {}
        for key in (
            "auto_transcode",
            "max_fps",
            "max_bitrate_mbps",
            "cache_budget_mb",
            "aggregate_cache_budget_mb",
            "readahead_seconds",
        ):
            item = policy.get(key)
            if isinstance(item, (bool, int, float, str)) and not isinstance(item, bytes):
                safe_policy[key] = item
        summary["playback_policy"] = safe_policy
    return summary


def _has_valid_stats(stats_path: Path | None) -> bool:
    if stats_path is None:
        return False
    try:
        value = json.loads(stats_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    if not isinstance(value, dict) or not value:
        return False
    cells = value.get("cells")
    if not isinstance(cells, list) or not cells:
        return False
    if "n_cells" in value and (
        isinstance(value["n_cells"], bool)
        or not isinstance(value["n_cells"], int)
        or value["n_cells"] != len(cells)
    ):
        return False
    for cell in cells:
        cell_id = cell.get("cell") if isinstance(cell, dict) else None
        if (
            not isinstance(cell, dict)
            or isinstance(cell_id, bool)
            or not isinstance(cell_id, int)
            or cell_id < 0
        ):
            return False
        if not isinstance(cell.get("totals"), dict) or not isinstance(cell.get("info"), dict):
            return False
        if "freezes" not in cell or "freeze_seconds" not in cell:
            return False
        if not _finite_number(cell["freezes"], nonnegative=True):
            return False
        if not _finite_number(cell["freeze_seconds"], nonnegative=True):
            return False
    return True


def _gate(status: str, value: Any, note: str) -> dict[str, Any]:
    return {"status": status, "value": value, "note": note}


def analyze_run(
    report_dir: str | Path,
    *,
    expected_cells: int | None = None,
    expected_duration_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Analyze a completed run and emit a machine-readable gate report."""
    if expected_cells is not None and (
        isinstance(expected_cells, bool)
        or not isinstance(expected_cells, int)
        or expected_cells < 1
    ):
        raise ValueError("expected_cells must be a positive integer")
    if expected_duration_seconds is not None and (
        not _finite_number(expected_duration_seconds)
        or expected_duration_seconds <= 0
    ):
        raise ValueError("expected_duration_seconds must be positive and finite")
    root = _reject_symlink_components(report_dir)
    if not root.is_dir():
        raise ValueError("analysis report must be a directory")
    log_path = _find_file(root, ("hyperwall.log", "hyperwall-7.log"))
    jsonl_candidates = sorted(_safe_children(root, "hyperwall_soak_*.jsonl"))
    jsonl_paths_seen = sorted(root.glob("hyperwall_soak_*.jsonl"))
    jsonl_path = jsonl_candidates[0] if len(jsonl_candidates) == 1 and len(jsonl_paths_seen) == 1 else None
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path else ""
    stats_candidates = sorted(_safe_children(root, "hyperwall_stats_*.json"))
    stats_path = stats_candidates[0] if len(stats_candidates) == 1 else None
    stats_valid = _has_valid_stats(stats_path)
    stats_summary = _stats_summary(stats_path)
    manifest = (
        parse_soak_jsonl(jsonl_path.read_text(encoding="utf-8", errors="replace"))
        if jsonl_path else {}
    )
    parsed = parse_app_log(log)
    gates: dict[str, dict[str, Any]] = {}
    missing_log = log_path is None
    empty_log = log_path is not None and not log.strip()
    gates["log_presence"] = _gate(
        "WARNING" if missing_log else "BLOCK" if empty_log else "PASS",
        {"path": str(log_path) if log_path else None, "bytes": len(log.encode("utf-8"))},
        "A completed phase requires a non-empty application log.",
    )
    gates["malformed_numeric_fields"] = _gate(
        "BLOCK" if parsed.get("malformed_numeric_fields", 0) else "WARNING" if missing_log else "PASS",
        parsed.get("malformed_numeric_fields", 0),
        "Malformed or non-finite numeric log fields block a clean verdict.",
    )
    gates["stats_presence"] = _gate(
        "WARNING" if not log_path and not jsonl_path else "PASS" if stats_valid else "BLOCK",
        str(stats_path) if stats_path else None,
        "A completed phase requires one valid final stats artifact.",
    )
    if expected_cells is not None:
        observed_cells = stats_summary.get("n_cells")
        gates["cell_count"] = _gate(
            "PASS"
            if stats_valid and observed_cells == expected_cells
            else "BLOCK",
            {"expected": expected_cells, "observed": observed_cells},
            "The final stats artifact must contain the expected number of cells.",
        )
    gates["freeze_count"] = _gate(
        "BLOCK" if parsed.get("freeze_count", 0) else "WARNING" if missing_log else "PASS",
        parsed.get("freeze_count", 0),
        "Any logged cache-starvation freeze blocks a clean media pass.",
    )
    gates["connection_refused"] = _gate(
        "BLOCK" if parsed.get("connection_refused", 0) else "WARNING" if missing_log else "PASS",
        parsed.get("connection_refused", 0),
        "Source connection refusals indicate an unhealthy run path.",
    )
    gates["hardware_decode_failures"] = _gate(
        "BLOCK" if parsed.get("hardware_decode_failures", 0) else "WARNING" if missing_log else "PASS",
        parsed.get("hardware_decode_failures", 0),
        "Hardware decode failures require a clean decoder path.",
    )
    gates["max_loop_stall_ms"] = _gate(
        "BLOCK" if parsed.get("max_loop_stall_ms", 0) > 500 else "WARNING" if missing_log else "PASS",
        parsed.get("max_loop_stall_ms", 0),
        "GUI stalls above 500 ms block the responsiveness gate.",
    )
    growth = manifest.get("working_set_growth_mb")
    growth_is_large = isinstance(growth, (int, float)) and growth > 1024
    peak_rss_only = manifest.get("ws_metric") == "peak_rss_mb"
    growth_status = (
        "WARNING"
        if growth_is_large and peak_rss_only
        else "BLOCK"
        if growth_is_large
        else "WARNING"
        if growth is None
        else "PASS"
    )
    growth_note = (
        "Peak RSS is a high-water mark; corroborate with current RSS or allocator "
        "evidence before calling it a leak."
        if peak_rss_only
        else "Growth above 1 GiB requires investigation; missing RSS is not zero."
    )
    gates["working_set_growth_mb"] = _gate(
        growth_status,
        growth,
        growth_note,
    )
    invariant = manifest.get("invariant_violations")
    invariant_number: int | float | None = None
    if isinstance(invariant, (int, float)) and not isinstance(invariant, bool):
        if math.isfinite(float(invariant)) and invariant >= 0:
            invariant_number = invariant
    invariant_valid = (
        invariant_number is not None
        and invariant_number == int(invariant_number)
    )
    gates["invariant_violations"] = _gate(
        "PASS" if invariant_valid and invariant == 0 else (
            "BLOCK" if invariant_valid else "WARNING" if invariant is None else "BLOCK"
        ),
        invariant,
        "State invariants must be zero and present.",
    )
    gates["manifest_shape"] = _gate(
        "BLOCK" if len(jsonl_candidates) > 1 else (
        "WARNING" if not jsonl_path else (
            "BLOCK" if (
                manifest.get("malformed_records", 0)
                or manifest.get("start_event_count") != 1
                or manifest.get("finish_event_count") != 1
            ) else "PASS"
        )
        ),
        {
            "candidates": len(jsonl_candidates),
            "malformed_records": manifest.get("malformed_records") if manifest else None,
            "start_event_count": manifest.get("start_event_count") if manifest else None,
            "finish_event_count": manifest.get("finish_event_count") if manifest else None,
        },
        "Exactly one valid JSONL soak manifest is required.",
    )
    missing_required = (
        not manifest
        or not manifest.get("start_event_present", False)
        or manifest.get("duration_seconds") is None
        or not manifest.get("finish_event_present", False)
        or manifest.get("start_event_count") != 1
        or manifest.get("finish_event_count") != 1
        or manifest.get("invariant_violations") is None
        or not _finite_number(manifest.get("sample_count", 0), nonnegative=True)
        or manifest.get("sample_count", 0) < 1
        or manifest.get("ws_metric") == "mixed"
        or not _valid_event_shape({
            "baseline": {"ws_mb": manifest.get("baseline_ws_mb")},
            "resources": {"ws_mb": manifest.get("final_ws_mb")},
            "duration_seconds": manifest.get("duration_seconds"),
            "sample_count": manifest.get("sample_count", 0),
        })
        or any(not _valid_sample_record(sample) for sample in manifest.get("samples", []))
    )
    gates["required_events"] = _gate(
        "WARNING" if not jsonl_path else "BLOCK" if missing_required else "PASS",
        {
            "duration_seconds": manifest.get("duration_seconds") if manifest else None,
            "invariant_violations": manifest.get("invariant_violations") if manifest else None,
            "finish_event_present": manifest.get("finish_event_present", False) if manifest else False,
        },
        "A completed run requires finish duration and invariant evidence.",
    )
    if expected_duration_seconds is not None:
        observed_duration = manifest.get("duration_seconds") if manifest else None
        duration_valid = _finite_number(observed_duration, nonnegative=True)
        observed_number = (
            float(observed_duration)
            if duration_valid and isinstance(observed_duration, (int, float))
            else None
        )
        coverage = (
            observed_number / float(expected_duration_seconds)
            if observed_number is not None else None
        )
        gates["duration_coverage"] = _gate(
            "PASS" if coverage is not None and coverage >= 0.95 else "BLOCK",
            {
                "observed_seconds": observed_duration,
                "expected_seconds": expected_duration_seconds,
                "coverage": round(coverage, 4) if coverage is not None else None,
            },
            "Active soak duration must cover at least 95% of the requested interval; "
            "sleep/suspend gaps invalidate the measurement.",
        )
    for key, note in (
        ("playback_errors", "Playback errors must be zero."),
        ("retry_skips", "Exhausted playback retries must be zero."),
        ("crash_loop_guard", "Crash-loop guard activations must be zero."),
        ("hls_segment_failures", "HLS segment failures must be zero."),
        ("stream_open_failures", "Stream-open failures must be zero."),
        ("video_decode_errors", "Video decode errors must be zero."),
        ("audio_decode_errors", "Audio decode errors must be zero."),
        ("av_desync", "A/V desynchronization must be zero."),
        ("audio_underrun", "Audio underruns must be zero."),
        ("decoder_buffer_warnings", "Decoder buffer warnings must be zero."),
        ("slow_slots", "Slow GUI slots must be zero."),
    ):
        value = parsed.get(key, 0)
        gates[key] = _gate(
            "BLOCK" if value else "WARNING" if missing_log else "PASS",
            value,
            note,
        )
    statuses = [gate["status"] for gate in gates.values()]
    verdict = "BLOCK" if "BLOCK" in statuses else (
        "WARNING" if "WARNING" in statuses else "PASS"
    )
    return {
        "verdict": verdict,
        "presentation_quality": "unmeasured_without_capture",
        "report_dir": str(root),
        "files": {
            "log": str(log_path) if log_path else None,
            "jsonl": str(jsonl_path) if jsonl_path else None,
            "stats": str(stats_path) if stats_path else None,
        },
        "stats": stats_summary,
        "manifest": manifest,
        "log": parsed,
        "gates": gates,
    }


def write_redacted_copy(source: str | Path, destination: str | Path) -> None:
    """Write a redacted text copy without exposing credentials in reports."""
    source_path = _reject_symlink_components(source)
    destination_path = _reject_symlink_components(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_components(destination_path.parent)
    if destination_path.parent.is_symlink():
        raise ValueError("redacted destination parent must not be a symlink")
    force_private_permissions(destination_path.parent, 0o700)
    source_fd = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(source_fd, "r", encoding="utf-8", errors="replace") as source_file:
        text = source_file.read()
    if source_path.suffix.lower() in {".json", ".jsonl"}:
        if source_path.suffix.lower() == ".jsonl":
            output_lines = []
            for line in text.splitlines():
                try:
                    output_lines.append(json.dumps(redact_json_value(json.loads(line))))
                except json.JSONDecodeError:
                    if _contains_sensitive_fragment(line):
                        raise ValueError("unredactable malformed JSONL credential record")
                    output_lines.append(redact_text(line))
            text = "\n".join(output_lines) + ("\n" if output_lines else "")
        else:
            try:
                text = json.dumps(redact_json_value(json.loads(text)), indent=2) + "\n"
            except json.JSONDecodeError:
                if _contains_sensitive_fragment(text):
                    raise ValueError("unredactable malformed JSON credential record")
                text = redact_text(text)
    else:
        text = redact_text(text)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    destination_fd = os.open(destination_path, flags, 0o600)
    with os.fdopen(destination_fd, "w", encoding="utf-8") as destination_file:
        destination_file.write(text)
    force_private_permissions(destination_path, 0o600)


def redact_tree(report_dir: str | Path, destination: str | Path) -> None:
    """Copy text diagnostics to a redacted directory; never copy images/video."""
    source = _reject_symlink_components(report_dir)
    if not source.is_dir():
        raise ValueError("redacted source must be a directory")
    target = _reject_symlink_components(destination)
    if target == source or source in target.parents:
        raise ValueError("redacted destination must be outside the source directory")
    if target.exists() and target.is_dir():
        _reject_existing_symlink_children(target)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_components(target)
    force_private_permissions(target, 0o700)
    allowed_suffixes = {".env", ".json", ".jsonl", ".log", ".txt"}
    for path in source.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in allowed_suffixes:
            continue
        write_redacted_copy(path, target / path.name)
