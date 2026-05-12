"""Run HyperWall repo guard tests without requiring pytest.

Usage:
    python tests/run_repo_guards.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def main() -> int:
    test_path = Path(__file__).with_name("test_repo_guards.py")
    spec = importlib.util.spec_from_file_location("test_repo_guards", test_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {test_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    tests = sorted(name for name in dir(module) if name.startswith("test_"))
    for name in tests:
        getattr(module, name)()
        print(f"PASS {name}")
    print(f"\n{len(tests)} repo guard test(s) passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
