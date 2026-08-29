#!/usr/bin/env python3
"""Build a sanitized bounded macOS native-profile report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
    parser.add_argument("--output", help="write JSON report to this path")
    args = parser.parse_args(argv)

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
