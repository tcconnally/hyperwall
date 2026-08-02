"""Hyperwall — platform adaptation tests (macOS port).

Pure logic, no PyQt/mpv/Emby: verifies the platform MPV_OPTS matrix and the
wid masking rule that keeps 64-bit NSView pointers intact off Windows.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def test_01_macos_opts_use_render_api():
    """macOS: vo=libmpv (render API), videotoolbox, coreaudio, no gpu_api."""
    from hyperwall.constants import mpv_opts_for_platform
    opts = mpv_opts_for_platform("darwin")
    # --wid embedding is unsupported by mpv's Swift macOS backend; cells
    # render via the libmpv render API into QOpenGLWidgets (macembed.py).
    assert opts["vo"] == "libmpv", opts["vo"]
    assert opts["hwdec"] == "videotoolbox", opts["hwdec"]
    assert "gpu_api" not in opts, "d3d11 gpu_api leaked into macOS opts"
    assert str(opts["ao"]).startswith("coreaudio"), opts["ao"]
    # Fill every cell edge-to-edge. panscan preserves aspect ratio and crops
    # overflow instead of introducing black bars in portrait/narrow grids.
    assert opts["panscan"] == 1.0
    # The render call must never block the single GUI thread on the audio
    # clock (8 cells x 50ms would serialize into wall-wide jank).
    assert opts["video_timing_offset"] == 0


def test_02_windows_opts_unchanged():
    """Windows: the tuned d3d11 path must survive the refactor verbatim."""
    from hyperwall.constants import mpv_opts_for_platform
    opts = mpv_opts_for_platform("win32")
    assert opts["vo"] == "gpu-next"
    assert opts["gpu_api"] == "d3d11"
    assert opts["hwdec"] == "d3d11va"
    assert str(opts["ao"]).startswith("wasapi")
    assert opts["panscan"] == 1.0
    # HQ downscaling is load-bearing on every platform.
    assert opts["dscale"] == "mitchell"
    assert opts["correct_downscaling"] == "yes"


def test_03_linux_opts_are_sane():
    """Linux: no d3d11 gpu_api, auto-safe hwdec (CI/headless sanity)."""
    from hyperwall.constants import mpv_opts_for_platform
    opts = mpv_opts_for_platform("linux")
    assert "gpu_api" not in opts
    assert opts["hwdec"] == "auto-safe"
    assert "ao" not in opts


def test_04_native_wid_masking():
    """The 32-bit HWND mask must never touch a 64-bit pointer."""
    from hyperwall.constants import native_wid
    # Windows HWNDs need the sign-extension mask...
    assert native_wid(0xFFFFFFFF80012345, "win32") == 0x80012345
    # ...but the same mask would corrupt a 64-bit NSView*/Window pointer.
    assert native_wid(0x00007FF012345678, "darwin") == 0x00007FF012345678
    assert native_wid(0x00007FF012345678, "linux") == 0x00007FF012345678


def test_05_env_overrides_still_win_on_macos():
    """HYPERWALL_HWDEC etc. override the platform defaults (escape hatch)."""
    from hyperwall.constants import apply_env_overrides, mpv_opts_for_platform
    os.environ["HYPERWALL_HWDEC"] = "videotoolbox-copy"
    try:
        opts = apply_env_overrides(mpv_opts_for_platform("darwin"))
    finally:
        del os.environ["HYPERWALL_HWDEC"]
    assert opts["hwdec"] == "videotoolbox-copy"


def run_all() -> int:
    tests = [
        test_01_macos_opts_use_render_api,
        test_02_windows_opts_unchanged,
        test_03_linux_opts_are_sane,
        test_04_native_wid_masking,
        test_05_env_overrides_still_win_on_macos,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {test.__name__}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    sys.exit(run_all())
