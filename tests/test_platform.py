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
    """Larger macOS host: libmpv render API + VideoToolbox + CoreAudio."""
    from hyperwall.constants import mpv_opts_for_platform
    opts = mpv_opts_for_platform("darwin", physical_memory_mb=24 * 1024)
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


def test_06_macos_render_api_prefers_display_resample_sync():
    from hyperwall.constants import mpv_opts_for_platform
    opts = mpv_opts_for_platform("darwin")
    assert opts["video_sync"] == "display-resample"


def test_07_small_macos_host_defaults_to_software_decode():
    """The fixed 16 GiB M5 wall must avoid its measured VideoToolbox faults."""
    from hyperwall.constants import mpv_opts_for_platform
    assert mpv_opts_for_platform("darwin", physical_memory_mb=16 * 1024)["hwdec"] == "no"
    assert mpv_opts_for_platform("darwin", physical_memory_mb=24 * 1024)["hwdec"] == "videotoolbox"


def test_08_macos_low_cost_render_profile_is_explicit_and_scoped():
    from hyperwall.constants import apply_render_profile, mpv_opts_for_platform

    hq = mpv_opts_for_platform("darwin", physical_memory_mb=16 * 1024)
    low = apply_render_profile(hq, "low-cost", platform="darwin")

    assert hq["dscale"] == "mitchell"
    assert hq["scale"] == "ewa_lanczossharp"
    assert hq["deband"] == "yes"
    assert low["dscale"] == "bilinear"
    assert low["scale"] == "bilinear"
    assert low["deband"] == "no"
    assert low["correct_downscaling"] == "no"


def test_09_low_cost_profile_does_not_change_non_macos_options():
    from hyperwall.constants import apply_render_profile, mpv_opts_for_platform

    windows = mpv_opts_for_platform("win32")
    assert apply_render_profile(windows, "low-cost", platform="win32") == windows
    linux = mpv_opts_for_platform("linux")
    assert apply_render_profile(linux, "low-cost", platform="linux") == linux


def test_10_unknown_render_profile_is_a_noop():
    from hyperwall.constants import apply_render_profile, mpv_opts_for_platform

    options = mpv_opts_for_platform("darwin")
    assert apply_render_profile(options, "not-a-profile", platform="darwin") == options


def run_all() -> int:
    tests = [
        test_01_macos_opts_use_render_api,
        test_02_windows_opts_unchanged,
        test_03_linux_opts_are_sane,
        test_04_native_wid_masking,
        test_05_env_overrides_still_win_on_macos,
        test_06_macos_render_api_prefers_display_resample_sync,
        test_07_small_macos_host_defaults_to_software_decode,
        test_08_macos_low_cost_render_profile_is_explicit_and_scoped,
        test_09_low_cost_profile_does_not_change_non_macos_options,
        test_10_unknown_render_profile_is_a_noop,
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
