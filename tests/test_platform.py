"""Mac-native platform policy tests.

These tests are pure logic. They do not require Qt, libmpv, or a macOS host.
"""
from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def test_01_macos_opts_use_render_api():
    from hyperwall.constants import macos_mpv_opts

    opts = macos_mpv_opts(profile="hardware")
    assert opts["vo"] == "libmpv"
    assert opts["hwdec"] == "videotoolbox"
    assert "gpu_api" not in opts
    assert str(opts["ao"]).startswith("coreaudio")
    assert opts["panscan"] == 1.0
    assert opts["video_timing_offset"] == 0
    assert opts["video_sync"] == "display-resample"


def test_02_decoder_profile_is_explicit_not_ram_selected():
    from hyperwall.constants import macos_mpv_opts

    safe_16 = macos_mpv_opts(profile="safe", physical_memory_mb=16 * 1024)
    safe_24 = macos_mpv_opts(profile="safe", physical_memory_mb=24 * 1024)
    copy_16 = macos_mpv_opts(profile="hardware-copy", physical_memory_mb=16 * 1024)

    assert safe_16["hwdec"] == "no"
    assert safe_24["hwdec"] == "no"
    assert copy_16["hwdec"] == "videotoolbox-copy"


def test_03_env_decoder_override_is_preserved():
    from hyperwall.constants import apply_env_overrides, macos_mpv_opts

    old = os.environ.get("HYPERWALL_HWDEC")
    os.environ["HYPERWALL_HWDEC"] = "videotoolbox-copy"
    try:
        opts = apply_env_overrides(macos_mpv_opts(profile="safe"))
    finally:
        if old is None:
            os.environ.pop("HYPERWALL_HWDEC", None)
        else:
            os.environ["HYPERWALL_HWDEC"] = old
    assert opts["hwdec"] == "videotoolbox-copy"


def test_04_cache_budget_is_m5_air_bounded():
    from hyperwall.constants import apply_cache_budget, macos_cache_defaults, macos_mpv_opts

    per_cell, total = macos_cache_defaults(physical_memory_mb=16 * 1024)
    opts = apply_cache_budget(
        macos_mpv_opts(profile="safe"),
        8,
        physical_memory_mb=16 * 1024,
    )

    assert (per_cell, total) == (256, 2048)
    assert opts["demuxer_max_bytes"] == "256MiB"
    assert opts["demuxer_readahead_secs"] == 30
    assert opts["cache_secs"] == 30


def test_05_low_cost_render_profile_is_explicit():
    from hyperwall.constants import apply_render_profile, macos_mpv_opts

    hq = macos_mpv_opts(profile="safe")
    low = apply_render_profile(hq, "low-cost")

    assert hq["dscale"] == "mitchell"
    assert hq["scale"] == "ewa_lanczossharp"
    assert hq["deband"] == "yes"
    assert low["dscale"] == "bilinear"
    assert low["scale"] == "bilinear"
    assert low["deband"] == "no"
    assert low["correct_downscaling"] == "no"


def test_06_unknown_render_profile_is_a_noop():
    from hyperwall.constants import apply_render_profile, macos_mpv_opts

    options = macos_mpv_opts(profile="safe")
    assert apply_render_profile(options, "not-a-profile") == options


def run_all() -> int:
    tests = [
        test_01_macos_opts_use_render_api,
        test_02_decoder_profile_is_explicit_not_ram_selected,
        test_03_env_decoder_override_is_preserved,
        test_04_cache_budget_is_m5_air_bounded,
        test_05_low_cost_render_profile_is_explicit,
        test_06_unknown_render_profile_is_a_noop,
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
