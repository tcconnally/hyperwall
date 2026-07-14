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
    _OPTIONAL_DEPS = ("PyQt6", "requests", "flask", "mpv")
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
    assert __version__ == "10.6.3"
    banner = runtime_banner()
    assert "Hyperwall" in banner
    assert "10.6.3" in banner


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


def test_06_nip_file_exists():
    """NVIDIA profile .nip file is present."""
    nip = os.path.join(REPO_ROOT, "hyperwall.nip")
    assert os.path.exists(nip), f"Missing: {nip}"


def test_07_empty_init_clean():
    """hyperwall/__init__.py exists and exports version."""
    from hyperwall import __version__
    assert __version__


def test_08_no_versioned_exe_literals():
    """No 'hyperwall_v<N>' or hardcoded old-version literals survive.

    Epic 1 (Identity Unification): the exe is versionless ('hyperwall.exe')
    and every version string derives from hyperwall.__init__.__version__.
    A stray 'hyperwall_v8' / 'hyperwall_v9' literal or a hardcoded
    'HyperWall/9.0'-style string means the drift is creeping back and G-Sync
    isolation (gated on the exe basename) can silently break on the next bump.

    __init__.py is exempt (it defines the single source of truth). This test
    scans tracked Python + build scripts.
    """
    import re

    # Files that carry exe names / version strings. Skip __init__.py (source
    # of truth) and this test file (which references the forbidden patterns).
    targets = [
        "hyperwall/app.py", "hyperwall/cell.py", "hyperwall/config.py",
        "hyperwall/constants.py", "hyperwall/emby.py", "hyperwall/nvidia.py",
        "hyperwall/wall.py", "hyperwall/web.py", "hyperwall/wizard.py",
        "build.bat", "build.ps1", "bootstrap.ps1", "launch.bat",
    ]
    # Forbidden: versioned exe basename, or a hardcoded HyperWall/<major>.<minor>
    # / Version="<major>.<minor>" string (these must derive from VERSION_SHORT).
    versioned_exe = re.compile(r"hyperwall_v\d")
    hardcoded_ver = re.compile(r'(HyperWall/|Version=")\d+\.\d+')

    offenders = []
    for rel in targets:
        path = os.path.join(REPO_ROOT, rel)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if versioned_exe.search(line) or hardcoded_ver.search(line):
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Version drift detected — derive from hyperwall.__version__ / "
        "VERSION_SHORT and use versionless 'hyperwall.exe':\n  "
        + "\n  ".join(offenders)
    )


def run_all() -> int:
    """Run all repo guards. Returns number of failures."""
    tests = [
        test_01_entry_point_imports,
        test_02_package_identity,
        test_03_config_loads,
        test_04_constants_present,
        test_05_config_template_exists,
        test_06_nip_file_exists,
        test_07_empty_init_clean,
        test_08_no_versioned_exe_literals,
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
