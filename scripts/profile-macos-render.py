#!/usr/bin/env python3
"""Build a sanitized bounded macOS native-profile report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hyperwall.capacity_policy import capacity_profile_from_analysis, select_capacity
from hyperwall.macos_profile import parse_powermetrics, parse_sample_stacks


def _read(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--powermetrics", help="text-mode powermetrics output")
    parser.add_argument("--sample", help="macOS sample output")
    parser.add_argument("--process-name", default="Python")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--matrix", nargs="+", metavar="PROFILE_JSON")
    parser.add_argument("--output", help="write JSON report to this path")
    args = parser.parse_args(argv)

    if args.matrix:
        profiles = []
        invalid_files = []
        for filename in args.matrix:
            try:
                value = json.loads(Path(filename).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                invalid_files.append(filename)
                continue
            if isinstance(value, dict) and "log" in value and "gates" in value:
                native_profile = value.get("native_profile")
                value = capacity_profile_from_analysis(
                    value,
                    native_profile=native_profile if isinstance(native_profile, dict) else None,
                )
            profiles.append(value)
        capacity = select_capacity(profiles)
        if invalid_files:
            capacity = dict(capacity)
            capacity["status"] = "BLOCK"
            capacity["invalid_files"] = sorted(invalid_files)
        report = {"capacity": capacity, "profiles": profiles}
        serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(serialized, encoding="utf-8")
        else:
            print(serialized, end="")
        return 0 if capacity["status"] == "PASS" else 2

    report = {
        "powermetrics": parse_powermetrics(
            _read(args.powermetrics),
            process_name=args.process_name,
            process_pid=args.pid,
        ),
        "sample": parse_sample_stacks(_read(args.sample)),
    }
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
    else:
        print(serialized, end="")
    return 0 if all(section["complete"] for section in report.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
