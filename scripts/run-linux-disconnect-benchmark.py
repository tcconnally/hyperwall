#!/usr/bin/env python3
"""Run an eight-cell Linux decode/transport benchmark that survives KVM loss.

This is deliberately not the physical-display qualification. Each cell uses
mpv's null video/audio outputs, so a KVM switching away from HDMI/DP cannot
tear down the benchmark. DRM connector changes, GPU samples, suspend gaps, and
per-cell mpv errors are recorded. A PASS here only covers decode/transport and
KVM-disconnect resilience; presentation remains BLOCK until a physical scanout
run passes separately.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hyperwall.linux_disconnect_benchmark import (  # noqa: E402
    build_mpv_command,
    clock_sample,
    connector_summary,
    parse_mpv_errors,
    read_drm_status,
    safe_input_label,
    summarize_run,
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _terminate_process_group(process: subprocess.Popen[Any], sig: int = signal.SIGTERM) -> None:
    """Terminate a cell and wrappers such as systemd-inhibit as one unit."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        try:
            process.terminate()
        except ProcessLookupError:
            pass


def _redact_text(text: str, sources: list[str], labels: list[str]) -> str:
    """Remove exact source arguments before any log becomes a report artifact."""
    redacted = text
    for source, label in zip(sources, labels):
        redacted = redacted.replace(source, f"<{label}>")
    return redacted


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _append_event(path: Path, kind: str, **payload: object) -> None:
    event = {"ts": _timestamp(), "kind": kind, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")
    path.chmod(0o600)


def _gpu_sample(binary: str) -> dict[str, Any]:
    query = "name,driver_version,memory.used,utilization.gpu,temperature.gpu,power.draw"
    try:
        result = subprocess.run(
            [binary, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "error_type": type(exc).__name__}
    if result.returncode != 0:
        return {"status": "error", "returncode": result.returncode}
    rows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return {"status": "ok", "rows": rows[:4]}


def _prepare_report(path: Path) -> None:
    if path.exists() and path.is_symlink():
        raise ValueError("report directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise ValueError("report path is not a directory")
    for child in path.iterdir():
        if child.name not in {".keep"}:
            raise ValueError("report directory must be empty before a benchmark run")
    path.chmod(0o700)


def _keep_awake_available(binary: str) -> bool:
    try:
        result = subprocess.run(
            [
                binary,
                "--what=idle:sleep",
                "--mode=block",
                "--why=Hyperwall Linux eight-cell benchmark preflight",
                "true",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run display-independent eight-cell Linux mpv playback diagnostics."
    )
    parser.add_argument(
        "--input",
        dest="sources",
        action="append",
        required=True,
        help="One source URL/path, or exactly one source per cell; values are never written to reports.",
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cells", type=int, default=8)
    parser.add_argument("--duration-s", type=float, default=120.0)
    parser.add_argument("--poll-s", type=float, default=2.0)
    parser.add_argument("--mpv", default=os.environ.get("HYPERWALL_MPV", "mpv"))
    parser.add_argument(
        "--hwdec",
        default=os.environ.get("HYPERWALL_LINUX_BENCHMARK_HWDEC", "auto-safe"),
    )
    parser.add_argument("--nvidia-smi", default=os.environ.get("HYPERWALL_NVIDIA_SMI", "nvidia-smi"))
    parser.add_argument("--drm-root", type=Path, default=Path("/sys/class/drm"))
    parser.add_argument(
        "--keep-awake",
        action="store_true",
        help="Wrap each mpv cell in systemd-inhibit idle/sleep blocking.",
    )
    parser.add_argument(
        "--require-disconnect",
        action="store_true",
        help="Fail the KVM-resilience sub-gate unless a connected-to-disconnected transition is observed.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.cells < 1 or args.cells > 32:
        parser.error("--cells must be between 1 and 32")
    if args.duration_s <= 0 or args.poll_s <= 0:
        parser.error("--duration-s and --poll-s must be positive")
    if len(args.sources) not in (1, args.cells):
        parser.error("provide exactly one --input or exactly one --input per cell")
    if args.keep_awake and shutil.which("systemd-inhibit") is None:
        parser.error("--keep-awake requires systemd-inhibit")
    mpv_binary = shutil.which(args.mpv) or (args.mpv if Path(args.mpv).is_file() else None)
    if mpv_binary is None:
        print(json.dumps({"status": "BLOCK", "reason": "mpv_not_found"}, sort_keys=True))
        return 2
    nvidia_binary = shutil.which(args.nvidia_smi) or (
        args.nvidia_smi if Path(args.nvidia_smi).is_file() else None
    )
    if nvidia_binary is None:
        print(json.dumps({"status": "BLOCK", "reason": "nvidia_smi_not_found"}, sort_keys=True))
        return 2
    if args.keep_awake and not _keep_awake_available("systemd-inhibit"):
        print(json.dumps({"status": "BLOCK", "reason": "keep_awake_unavailable"}, sort_keys=True))
        return 2

    try:
        _prepare_report(args.output)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "BLOCK", "reason": str(exc)}, sort_keys=True))
        return 2

    sources = args.sources if len(args.sources) == args.cells else args.sources * args.cells
    labels = [safe_input_label(source) for source in sources]
    events_path = args.output / "events.jsonl"
    raw_log_dir = Path(tempfile.mkdtemp(prefix=".raw-", dir=args.output))
    raw_log_dir.chmod(0o700)
    run_env = {
        "started_at": _timestamp(),
        "benchmark_mode": "linux_headless_decode_transport",
        "cells": args.cells,
        "duration_seconds": args.duration_s,
        "poll_seconds": args.poll_s,
        "hwdec": args.hwdec,
        "input_labels": labels,
        "keep_awake_requested": args.keep_awake,
        "require_disconnect": args.require_disconnect,
        "physical_presentation_gate": "BLOCK",
    }
    _write_json(args.output / "run.env.json", run_env)
    _append_event(events_path, "run_start", **run_env)

    interrupted = False

    def _stop_handler(_signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    child_records: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        log_path = raw_log_dir / f"cell-{index:02d}.log"
        command = build_mpv_command(
            mpv_binary,
            source,
            hwdec=args.hwdec,
            log_path=log_path,
        )
        if args.keep_awake:
            command = [
                "systemd-inhibit",
                "--what=idle:sleep",
                "--mode=block",
                "--why=Hyperwall Linux eight-cell benchmark",
                *command,
            ]
        env = os.environ.copy()
        env["LC_NUMERIC"] = "C"
        env["HYPERWALL_NO_RELAUNCH"] = "1"
        handle = log_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except (OSError, ValueError):
            handle.close()
            for record in child_records:
                process = record["process"]
                if process.poll() is None:
                    _terminate_process_group(process)
            for record in child_records:
                record["process"].wait(timeout=5)
                record["handle"].close()
            raise
        child_records.append(
            {
                "cell": index,
                "label": labels[index],
                "process": process,
                "handle": handle,
                "log_path": log_path,
                "early_exit": False,
                "normal_stop": False,
            }
        )
        _append_event(events_path, "cell_start", cell=index, input_label=labels[index])

    start = clock_sample()
    last_display: dict[str, str] | None = None
    display_events: list[dict[str, Any]] = []
    gpu_samples: list[dict[str, Any]] = []
    next_gpu = 0.0
    try:
        while not interrupted:
            now = clock_sample()
            elapsed = now["monotonic"] - start["monotonic"]
            connectors = read_drm_status(args.drm_root)
            if connectors != last_display:
                state = {
                    "elapsed_monotonic_seconds": round(elapsed, 3),
                    "connectors": connectors,
                    "summary": connector_summary(connectors),
                }
                display_events.append(state)
                _append_event(events_path, "display_state", **state)
                last_display = connectors
            if elapsed >= next_gpu:
                sample = _gpu_sample(nvidia_binary)
                sample["elapsed_monotonic_seconds"] = round(elapsed, 3)
                gpu_samples.append(sample)
                _append_event(events_path, "gpu_sample", **sample)
                next_gpu = elapsed + args.poll_s
            exited = False
            for record in child_records:
                returncode = record["process"].poll()
                if returncode is not None and elapsed < args.duration_s:
                    record["early_exit"] = True
                    exited = True
                    _append_event(
                        events_path,
                        "cell_exit",
                        cell=record["cell"],
                        returncode=returncode,
                        early_exit=True,
                    )
            if exited:
                break
            if elapsed >= args.duration_s:
                for record in child_records:
                    record["normal_stop"] = True
                break
            time.sleep(min(args.poll_s, max(0.05, args.duration_s - elapsed)))
    finally:
        end = clock_sample()
        for record in child_records:
            process = record["process"]
            if process.poll() is None:
                _terminate_process_group(process)
        for record in child_records:
            process = record["process"]
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            record["returncode"] = process.returncode
            record["handle"].close()

    cell_results: list[dict[str, Any]] = []
    for record in child_records:
        try:
            text = record["log_path"].read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        redacted_text = _redact_text(text, sources, labels)
        report_log = args.output / f"cell-{record['cell']:02d}.log"
        report_log.write_text(redacted_text, encoding="utf-8")
        report_log.chmod(0o600)
        cell_results.append(
            {
                "cell": record["cell"],
                "input_label": record["label"],
                "returncode": record.get("returncode"),
                "early_exit": record["early_exit"],
                "normal_stop": record["normal_stop"],
                "errors": parse_mpv_errors(text),
            }
        )

    raw_log_cleanup_error: str | None = None
    try:
        shutil.rmtree(raw_log_dir)
    except OSError as exc:
        raw_log_cleanup_error = type(exc).__name__

    summary = summarize_run(
        requested_seconds=args.duration_s,
        elapsed_monotonic=end["monotonic"] - start["monotonic"],
        elapsed_boottime=end["boottime"] - start["boottime"],
        cell_results=cell_results,
        display_events=display_events,
        expected_cells=args.cells,
    )
    disconnect_observed = summary["display_loss_transitions"] > 0
    if args.require_disconnect and not disconnect_observed:
        summary["headless_disconnect_resilience"] = "BLOCK"
    summary.update(
        {
            "status": (
                "completed"
                if not interrupted
                and raw_log_cleanup_error is None
                and summary["decode_transport_verdict"] == "PASS"
                and (not args.require_disconnect or disconnect_observed)
                else "BLOCK"
            ),
            "raw_log_cleanup": "PASS" if raw_log_cleanup_error is None else "BLOCK",
            "raw_log_cleanup_error": raw_log_cleanup_error,
            "interrupted": interrupted,
            "input_labels": labels,
            "gpu_samples": gpu_samples,
            "display_events": display_events,
            "cells": cell_results,
            "required_disconnect_observed": disconnect_observed,
            "ended_at": _timestamp(),
        }
    )
    _write_json(args.output / "summary.json", summary)
    _append_event(events_path, "run_finish", summary=summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
