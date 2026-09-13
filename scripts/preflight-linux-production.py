#!/usr/bin/env python3
"""Verify the exact NVIDIA GPU contract before starting the Linux wall."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(_REPO_ROOT))

from hyperwall.linux_preflight import (  # noqa: E402
    LinuxPreflightError,
    validate_gpu_inventory,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail closed unless the expected NVIDIA GPU is visible."
    )
    parser.add_argument(
        "--nvidia-smi",
        default=os.environ.get("HYPERWALL_NVIDIA_SMI", "nvidia-smi"),
    )
    parser.add_argument(
        "--expected-gpu",
        default=os.environ.get("HYPERWALL_EXPECTED_GPU", "RTX 5070 Ti"),
    )
    parser.add_argument(
        "--min-vram-mib",
        type=int,
        default=int(os.environ.get("HYPERWALL_MIN_GPU_VRAM_MIB", "12000")),
    )
    parser.add_argument("--timeout-s", type=float, default=5.0)
    parser.add_argument(
        "--required",
        action="store_true",
        help="explicit production mode; failures always return nonzero",
    )
    args = parser.parse_args(argv)
    try:
        probe = subprocess.run(
            [
                args.nvidia_smi,
                "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=args.timeout_s,
            check=False,
        )
        if probe.returncode != 0:
            detail = probe.stderr.strip() or f"exit {probe.returncode}"
            raise LinuxPreflightError(f"nvidia-smi failed: {detail}")
        record = validate_gpu_inventory(
            probe.stdout,
            expected_model=args.expected_gpu,
            min_vram_mib=args.min_vram_mib,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError, LinuxPreflightError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "gpu": {
                    "name": record.name,
                    "memory_mib": record.memory_mib,
                    "driver_version": record.driver_version,
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
