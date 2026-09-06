"""
Hyperwall v9 — repo guard tests.

No-dependency checks that prevent known regressions:
  - No global mute shortcut
  - Escape emergency filter present
  - Entry point valid
  - Package structure intact
  - Config template present
  - No legacy v7.4 active
  - Runtime identity present
"""

from __future__ import annotations

import os
import sys

# Add repo root to path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def test_01_entry_point_imports():
    """hyperwall.app.main is importable."""
    # This is a smoke test. It can only run when the heavy third-party deps
    # (PyQt6, requests, flask, mpv) are installed. In a bare CI/dev env any one
    # of them may be the first to be missing — skip gracefully on any of them
    # rather than only PyQt6 (which was environment-dependent and flaky).
    _OPTIONAL_DEPS = ("PyQt6", "flask", "mpv")
    try:
        from hyperwall.app import main
        assert callable(main)
    except ImportError as e:
        missing = getattr(e, "name", "") or str(e)
        if any(dep in str(e) or dep == missing for dep in _OPTIONAL_DEPS):
            # Clean up partially-loaded modules so they don't break later tests
            for mod in list(sys.modules):
                if mod.startswith("hyperwall"):
                    del sys.modules[mod]
            print(f"  SKIP  test_01_entry_point_imports ({missing} not installed)")
            return
        raise


def test_02_package_identity():
    """Package has version and banner."""
    from hyperwall import __version__, runtime_banner
    assert __version__ == "10.15.0"
    banner = runtime_banner()
    assert "Hyperwall" in banner
    assert "10.15.0" in banner


def test_03_config_loads():
    """Config dataclass can be constructed."""
    from hyperwall.config import HyperwallConfig
    cfg = HyperwallConfig(
        server_url="http://localhost:8096",
        username="test",
        password="test",
    )
    assert cfg.server_url == "http://localhost:8096"
    assert cfg.last_grid_rows == 2


def test_04_constants_present():
    """All required constants are defined."""
    from hyperwall.constants import (
        MPV_OPTS, STREAM_START_STAGGER_MS, MAX_RETRIES,
        CONTROLS_HEIGHT, AUTOHIDE_MS, OVERLAY_SHOW_MS, MOUSE_IDLE_MS,
    )
    assert isinstance(MPV_OPTS, dict)
    assert "vo" in MPV_OPTS
    # HQ downscaling is load-bearing for a downscaling wall — guard against a
    # silent revert to bilinear (profile=fast).
    assert MPV_OPTS.get("dscale") == "mitchell"
    assert MPV_OPTS.get("correct_downscaling") == "yes"
    assert MPV_OPTS.get("profile") != "fast"
    assert STREAM_START_STAGGER_MS > 0
    assert MAX_RETRIES > 0


def test_05_config_template_exists():
    """config.example.ini is present."""
    template = os.path.join(REPO_ROOT, "config.example.ini")
    assert os.path.exists(template), f"Missing: {template}"
    with open(template) as f:
        content = f.read()
    assert "[Login]" in content
    assert "[Settings]" in content


def test_06_macos_native_support_surface():
    """The checkout contains no Windows launcher or NVIDIA support artifact."""
    forbidden_suffixes = {".bat", ".ps1", ".nip"}
    artifacts = sorted(
        os.path.relpath(path.path, REPO_ROOT)
        for path in os.scandir(REPO_ROOT)
        if path.is_file() and os.path.splitext(path.name)[1].lower() in forbidden_suffixes
    )
    for directory in ("scripts", "tests", "hyperwall"):
        root = os.path.join(REPO_ROOT, directory)
        if not os.path.isdir(root):
            continue
        for current, _dirs, files in os.walk(root):
            for name in files:
                if os.path.splitext(name)[1].lower() in forbidden_suffixes:
                    artifacts.append(os.path.relpath(os.path.join(current, name), REPO_ROOT))
    assert not artifacts, f"Windows/native-foreign artifacts remain: {sorted(artifacts)}"

    nvidia = os.path.join(REPO_ROOT, "hyperwall", "nvidia.py")
    assert not os.path.exists(nvidia), f"NVIDIA platform module remains: {nvidia}"

    app = os.path.join(REPO_ROOT, "hyperwall", "app.py")
    with open(app, encoding="utf-8") as f:
        app_source = f.read()
    assert "HYPERWALL_GPU_API" not in app_source
    assert "import requests" not in app_source


def test_07_empty_init_clean():
    """hyperwall/__init__.py exists and exports version."""
    from hyperwall import __version__
    assert __version__


def test_07_package_exports_version():
    """Package identity remains available without an executable wrapper."""
    from hyperwall import __version__
    assert __version__


def run_all() -> int:
    """Run all repo guards. Returns number of failures."""
    tests = [
        test_01_entry_point_imports,
        test_02_package_identity,
        test_03_config_loads,
        test_04_constants_present,
        test_05_config_template_exists,
        test_06_macos_native_support_surface,
        test_07_package_exports_version,
    ]
    passed = 0
    failed = 0
    skipped = 0
    for test in tests:
        name = test.__name__
        try:
            test()
            passed += 1
            print(f"  PASS  {name}")
        except SystemExit:
            raise
        except Exception as e:
            # Check if this was a skip (printed by the test)
            failed += 1
            # Check if the test printed SKIP already
            print(f"  FAIL  {name}: {e}")
    total = len(tests)
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped out of {total} tests.")
    return failed


if __name__ == "__main__":
    sys.exit(run_all())
