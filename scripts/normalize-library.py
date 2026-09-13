#!/usr/bin/env python3
"""Build a verified wall-safe media library without deleting source media.

Examples:
  python3 scripts/normalize-library.py \
    --source /srv/media-original \
    --destination /srv/media-wall-safe

  python3 scripts/normalize-library.py \
    --source /srv/media-original \
    --destination /srv/media-wall-safe \
    --execute

The first form is a dry run. The second converts each source file to an
atomic H.264/AAC MP4 and verifies the result with ffprobe before publishing it.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(_REPO_ROOT))

from hyperwall.normalization import (  # noqa: E402
    NormalizationProfile,
    ffmpeg_args,
    ffprobe_args,
    iter_media_files,
    normalized_output_path,
    validate_probe,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a media tree into a verified HyperWall library."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument(
        "--execute", action="store_true",
        help="perform conversions; without this flag only print the plan",
    )
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--timeout-s", type=float, default=3600.0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--strict", action="store_true",
        help="return nonzero when any file cannot be planned or verified",
    )
    return parser


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _probe(binary: str, path: Path, timeout_s: float) -> tuple[dict[str, Any] | None, str | None]:
    try:
        result = subprocess.run(
            [binary, *ffprobe_args(path)[1:]],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"ffprobe_failed:{type(exc).__name__}"
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        return None, "ffprobe_failed:" + (detail[-1][:240] if detail else "exit")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "ffprobe_invalid_json"
    if not isinstance(payload, dict):
        return None, "ffprobe_invalid_shape"
    return payload, None


def _run_ffmpeg(
    binary: str, input_path: Path, output_path: Path,
    profile: NormalizationProfile, timeout_s: float,
) -> tuple[bool, str | None]:
    temp_path = output_path.with_name(
        output_path.stem + ".partial" + output_path.suffix
    )
    temp_path.unlink(missing_ok=True)
    args = ffmpeg_args(input_path, temp_path, profile)
    args[0] = binary
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        temp_path.unlink(missing_ok=True)
        return False, f"ffmpeg_failed:{type(exc).__name__}"
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        temp_path.unlink(missing_ok=True)
        return False, "ffmpeg_failed:" + (detail[-1][:240] if detail else "exit")
    if not temp_path.is_file() or temp_path.stat().st_size == 0:
        temp_path.unlink(missing_ok=True)
        return False, "ffmpeg_empty_output"
    os.replace(temp_path, output_path)
    return True, None


def _record(manifest_handle: Any, record: dict[str, Any]) -> None:
    if manifest_handle is not None:
        manifest_handle.write(json.dumps(record, sort_keys=True) + "\n")
        manifest_handle.flush()
    print(json.dumps(record, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source = args.source.expanduser().resolve()
    destination = args.destination.expanduser().resolve()
    if not source.is_dir():
        print(f"ERROR source is not a directory: {source}", file=sys.stderr)
        return 2
    if source == destination or _inside(destination, source):
        print("ERROR destination must be separate from and outside source", file=sys.stderr)
        return 2
    if args.timeout_s <= 0 or args.limit < 0:
        print("ERROR timeout and limit must be nonnegative (timeout positive)", file=sys.stderr)
        return 2

    profile = NormalizationProfile()
    manifest = args.manifest.expanduser().resolve() if args.manifest else None
    handle = None
    if args.execute:
        destination.mkdir(parents=True, exist_ok=True)
        if manifest is None:
            manifest = destination / ".hyperwall-normalization.jsonl"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        handle = manifest.open("a", encoding="utf-8")

    planned = converted = skipped = failed = 0
    seen_outputs: set[Path] = set()
    started = time.time()
    try:
        for index, input_path in enumerate(iter_media_files(source)):
            if args.limit and index >= args.limit:
                break
            output_path = normalized_output_path(input_path, source, destination)
            record: dict[str, Any] = {
                "input": str(input_path),
                "output": str(output_path),
                "profile": {
                    "max_width": profile.max_width,
                    "max_height": profile.max_height,
                    "max_fps": profile.max_fps,
                    "max_video_bitrate_mbps": profile.max_video_bitrate_mbps,
                },
            }
            if output_path in seen_outputs:
                record["status"] = "error"
                record["reason"] = "output_collision"
                failed += 1
                _record(handle, record)
                continue
            seen_outputs.add(output_path)
            planned += 1

            if not args.execute:
                record["status"] = "plan"
                _record(handle, record)
                continue

            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.is_file():
                probe, probe_error = _probe(args.ffprobe, output_path, args.timeout_s)
                if probe_error is None and probe is not None:
                    violations = validate_probe(probe, profile)
                    if not violations:
                        record["status"] = "skip_verified"
                        skipped += 1
                        _record(handle, record)
                        continue

            ok, error = _run_ffmpeg(
                args.ffmpeg, input_path, output_path, profile, args.timeout_s,
            )
            if not ok:
                record["status"] = "error"
                record["reason"] = error
                failed += 1
                _record(handle, record)
                continue
            probe, probe_error = _probe(args.ffprobe, output_path, args.timeout_s)
            violations = (
                [probe_error] if probe_error else validate_probe(probe or {}, profile)
            )
            if violations:
                record["status"] = "error"
                record["reason"] = "output_not_wall_safe"
                record["violations"] = violations
                failed += 1
                output_path.unlink(missing_ok=True)
            else:
                record["status"] = "converted_verified"
                converted += 1
            _record(handle, record)
    finally:
        if handle is not None:
            handle.close()

    summary = {
        "status": "ok" if failed == 0 else "blocked",
        "dry_run": not args.execute,
        "planned": planned,
        "converted": converted,
        "skipped_verified": skipped,
        "failed": failed,
        "elapsed_s": round(time.time() - started, 3),
    }
    print(json.dumps({"summary": summary}, sort_keys=True))
    return 1 if args.strict and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
