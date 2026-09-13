#!/usr/bin/env python3
"""Unload resident Ollama models before reserving GPU memory for HyperWall."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.fspath(_REPO_ROOT))

from hyperwall.ollama_control import (  # noqa: E402
    OllamaControlError,
    loaded_models,
    unload_loaded_models,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Unload resident Ollama models and verify /api/ps is empty."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
    )
    parser.add_argument("--wait-s", type=float, default=20.0)
    parser.add_argument("--timeout-s", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--required",
        action="store_true",
        help="return nonzero if the daemon cannot be verified empty",
    )
    args = parser.parse_args(argv)
    try:
        initial = loaded_models(args.url, timeout_s=args.timeout_s)
        if args.dry_run:
            print(json.dumps({"dry_run": True, "loaded": initial}, sort_keys=True))
            return 0
        remaining = unload_loaded_models(
            args.url,
            wait_s=args.wait_s,
            timeout_s=args.timeout_s,
        )
    except OllamaControlError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1 if args.required else 0
    result = {
        "status": "ok" if not remaining else "blocked",
        "initial": initial,
        "remaining": remaining,
    }
    print(json.dumps(result, sort_keys=True))
    return 1 if args.required and remaining else 0


if __name__ == "__main__":
    raise SystemExit(main())
