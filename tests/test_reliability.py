"""
Unit tests for hyperwall.reliability (Epic 2) and the constants that drive it.

Pure logic only — no PyQt / mpv / Emby. Runnable anywhere Python is.
Run: python tests/test_reliability.py
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from hyperwall.reliability import (  # noqa: E402
    apply_jitter,
    classify_playback_fault,
    count_recent,
    decoder_recovery_plan,
    end_file_reason,
    escalation_plan,
    is_stalled,
    is_systemic_outage,
    scale_demuxer_mb,
    should_park,
    transport_recovery_plan,
)


# ── is_stalled ────────────────────────────────────────────────────────────────

def test_stall_fires_after_threshold():
    assert is_stalled(25.0, paused=False, dragging=False, threshold_s=20)


def test_no_stall_within_threshold():
    assert not is_stalled(5.0, paused=False, dragging=False, threshold_s=20)


def test_paused_never_stalls():
    # A paused cell isn't making progress on purpose — must not be flagged.
    assert not is_stalled(999.0, paused=True, dragging=False, threshold_s=20)


def test_seeking_never_stalls():
    assert not is_stalled(999.0, paused=False, dragging=True, threshold_s=20)


def test_stall_boundary_is_strict():
    # Exactly at threshold is NOT stalled (must exceed).
    assert not is_stalled(20.0, paused=False, dragging=False, threshold_s=20)
    assert is_stalled(20.001, paused=False, dragging=False, threshold_s=20)


# ── count_recent / should_park ────────────────────────────────────────────────

def test_count_recent_window():
    times = [0.0, 10.0, 55.0, 58.0, 59.5]
    # now=60, window=60 → 0.0 is 60s ago (inclusive), all 5 count
    assert count_recent(times, now=60.0, window_s=60.0) == 5
    # window=10 → only 55,58,59.5 within last 10s
    assert count_recent(times, now=60.0, window_s=10.0) == 3


def test_should_park_triggers_on_threshold():
    times = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert should_park(times, now=6.0, window_s=60, threshold=5)


def test_should_not_park_below_threshold():
    times = [1.0, 2.0, 3.0]
    assert not should_park(times, now=6.0, window_s=60, threshold=5)


def test_old_failures_age_out_of_window():
    # Four ancient failures + one recent → not a loop.
    times = [1.0, 2.0, 3.0, 4.0, 500.0]
    assert not should_park(times, now=505.0, window_s=60, threshold=5)


# ── scale_demuxer_mb ──────────────────────────────────────────────────────────

def test_single_cell_gets_full_per_cell():
    assert scale_demuxer_mb(1, per_cell_mb=512, total_budget_mb=3072) == 512


def test_small_grid_under_budget_unscaled():
    # 4 cells * 512 = 2048 <= 3072 → no scaling.
    assert scale_demuxer_mb(4, per_cell_mb=512, total_budget_mb=3072) == 512


def test_large_grid_scaled_down():
    # 36 cells, 3072 budget → 3072/36 = 85 MiB each (well under 512).
    got = scale_demuxer_mb(36, per_cell_mb=512, total_budget_mb=3072)
    assert got == 85, got
    assert got * 36 <= 3072


def test_floor_respected_when_budget_can_afford_it():
    # The floor is useful when the aggregate budget can still pay for it.
    got = scale_demuxer_mb(64, per_cell_mb=10, total_budget_mb=3072, floor_mb=32)
    assert got == 32


def test_tiny_budget_does_not_exceed_aggregate_ceiling():
    got = scale_demuxer_mb(36, per_cell_mb=512, total_budget_mb=128, floor_mb=32)
    assert got * 36 <= 128


def test_zero_cells_is_safe():
    # Defensive: n<1 must not divide-by-zero.
    assert scale_demuxer_mb(0, per_cell_mb=512, total_budget_mb=3072) == 512


# ── decoder / transport fault containment ────────────────────────────────────

def test_fault_classifier_separates_decoder_and_transport_failures():
    assert classify_playback_fault(
        "hardware accelerator failed to decode picture"
    ) == "decoder"
    assert classify_playback_fault(
        "Connection reset by peer while reading partial file"
    ) == "transport"
    assert classify_playback_fault("audio device underrun detected") == "other"


def test_decoder_fault_falls_back_to_software_for_the_cell():
    plan = decoder_recovery_plan(
        fault_count=1, hardware_decode=True, max_faults=2,
    )
    assert plan == {"action": "fallback-software", "hwdec": "no"}


def test_repeated_software_decoder_fault_recreates_then_skips():
    assert decoder_recovery_plan(
        fault_count=1, hardware_decode=False, max_faults=2,
    )["action"] == "recreate"
    assert decoder_recovery_plan(
        fault_count=2, hardware_decode=False, max_faults=2,
    )["action"] == "skip"


def test_transport_fault_has_one_retry_then_quarantines_item():
    assert transport_recovery_plan(1, max_attempts=1) == {
        "action": "retry", "delay_s": 3,
    }
    assert transport_recovery_plan(2, max_attempts=1) == {
        "action": "skip", "delay_s": 0,
    }


# ── escalation_plan (retry → transcode → skip) ────────────────────────────────

def test_attempt1_retries_direct():
    p = escalation_plan(1, max_retries=3)
    assert p["action"] == "retry"
    assert p["transcode"] is False   # first attempt stays DIRECT
    assert p["delay_s"] == 2         # 2**1


def test_attempt2_escalates_to_transcode():
    p = escalation_plan(2, max_retries=3)
    assert p["action"] == "retry"
    assert p["transcode"] is True    # escalate at attempt >= 2
    assert p["delay_s"] == 4         # 2**2


def test_attempt3_still_retries_transcode():
    p = escalation_plan(3, max_retries=3)
    assert p["action"] == "retry"
    assert p["transcode"] is True
    assert p["delay_s"] == 8         # 2**3


def test_attempt_over_max_skips():
    p = escalation_plan(4, max_retries=3)
    assert p["action"] == "skip"
    assert p["transcode"] is False
    assert p["delay_s"] == 0


def test_full_escalation_sequence():
    # The exact retry→transcode→skip ladder a dead stream walks.
    seq = [escalation_plan(a, 3) for a in range(1, 5)]
    actions = [p["action"] for p in seq]
    transcodes = [p["transcode"] for p in seq]
    assert actions == ["retry", "retry", "retry", "skip"]
    assert transcodes == [False, True, True, False]


# ── apply_jitter ──────────────────────────────────────────────────────────────

def test_jitter_bounds():
    # rand=0 → 0.75x, rand→1 → 1.25x. Never outside that band.
    assert apply_jitter(4.0, 0.0) == 3.0
    assert abs(apply_jitter(4.0, 0.999999) - 5.0) < 0.01
    assert apply_jitter(4.0, 0.5) == 4.0


def test_jitter_clamps_bad_rand():
    # Defensive: out-of-range rand samples clamp instead of exploding delays.
    assert apply_jitter(4.0, -1.0) == 3.0
    assert apply_jitter(4.0, 2.0) == 5.0


def test_jitter_desynchronizes():
    # Two cells with different samples must not retry at the same instant.
    assert apply_jitter(8.0, 0.1) != apply_jitter(8.0, 0.9)


# ── is_systemic_outage ────────────────────────────────────────────────────────

def _events(*cells, t=100.0):
    return [(t, c) for c in cells]


def test_outage_majority_of_wall():
    # 8-cell wall: 4 distinct cells failing recently = majority → outage.
    ev = _events("a", "b", "c", "d")
    assert is_systemic_outage(ev, 100.0, window_s=45, total_cells=8)


def test_no_outage_below_majority():
    ev = _events("a", "b", "c")
    assert not is_systemic_outage(ev, 100.0, window_s=45, total_cells=8)


def test_repeat_failures_from_one_cell_dont_count_twice():
    # One flaky cell hammering retries is NOT an outage.
    ev = [(100.0, "a"), (101.0, "a"), (102.0, "a"), (103.0, "a"), (104.0, "a")]
    assert not is_systemic_outage(ev, 105.0, window_s=45, total_cells=8)


def test_old_failures_age_out():
    ev = [(10.0, "a"), (11.0, "b"), (12.0, "c"), (100.0, "d")]
    assert not is_systemic_outage(ev, 100.0, window_s=45, total_cells=8)


def test_small_walls_never_systemic():
    # 1–2 cell walls can't distinguish systemic vs bad media — keep plain
    # per-cell escalation there.
    ev = _events("a", "b")
    assert not is_systemic_outage(ev, 100.0, window_s=45, total_cells=2)


def test_min_cells_floor_on_small_majority():
    # 4-cell wall: majority is 2 but the floor is min_cells=3.
    ev = _events("a", "b")
    assert not is_systemic_outage(ev, 100.0, window_s=45, total_cells=4)
    ev = _events("a", "b", "c")
    assert is_systemic_outage(ev, 100.0, window_s=45, total_cells=4)


def test_outage_constants_defaults():
    from hyperwall import constants as c
    assert c.OUTAGE_WINDOW_S == 45
    assert c.OUTAGE_MIN_CELLS == 3
    assert c.OUTAGE_BACKOFF_S == 20
    assert c.MAX_DIRECT_FPS == 66
    assert c.MAX_DIRECT_BITRATE_MBPS == 60


# ── end_file_reason (python-mpv event decoding) ───────────────────────────────
# Shapes below match a live probe (2026-07-12) against the shipped mpv-2.dll
# + python-mpv 1.x: as_dict() carries reason as BYTES; data.reason is an int.

class _EvDict:
    """python-mpv 1.x shape: as_dict() with bytes values."""
    def __init__(self, reason):
        self._reason = reason

    def as_dict(self):
        return {"event": b"end-file", "reason": self._reason}


class _EvData:
    """python-mpv 1.x shape without as_dict: .data.reason int enum."""
    class _Data:
        def __init__(self, reason):
            self.reason = reason

    def __init__(self, reason):
        self.data = self._Data(reason)


class _EvLegacy:
    """Old python-mpv shape: .event is a plain dict."""
    def __init__(self, reason):
        self.event = {"reason": reason}


def test_reason_bytes_stop():
    assert end_file_reason(_EvDict(b"stop")) == "stop"


def test_reason_bytes_error():
    assert end_file_reason(_EvDict(b"error")) == "error"


def test_reason_bytes_eof():
    assert end_file_reason(_EvDict(b"eof")) == "eof"


def test_reason_int_enum():
    assert end_file_reason(_EvData(0)) == "eof"
    assert end_file_reason(_EvData(2)) == "stop"
    assert end_file_reason(_EvData(4)) == "error"
    assert end_file_reason(_EvData(5)) == "redirect"


def test_reason_unknown_int_defaults_eof():
    assert end_file_reason(_EvData(99)) == "eof"


def test_reason_legacy_dict():
    assert end_file_reason(_EvLegacy("eof")) == "eof"
    assert end_file_reason(_EvLegacy("stop")) == "stop"


def test_reason_garbage_defaults_eof():
    # The historic default — a shape we can't read must degrade, not crash.
    assert end_file_reason(object()) == "eof"
    assert end_file_reason(None) == "eof"


def test_reason_as_dict_raising_falls_through():
    class _Raises:
        def as_dict(self):
            raise RuntimeError("boom")
        class _Data:
            reason = 2
        data = _Data()
    assert end_file_reason(_Raises()) == "stop"


# ── constants env clamping (integration of _int_env) ──────────────────────────

def test_constants_defaults_load():
    from hyperwall import constants as c
    assert c.STALL_TIMEOUT_S == 20
    assert c.WATCHDOG_INTERVAL_MS == 5_000
    assert c.CRASH_LOOP_THRESHOLD == 5
    expected_per_cell, expected_budget = c.cache_defaults_for_platform(
        sys.platform, c._physical_memory_mb(),
    )
    assert c.DEMUXER_PER_CELL_MB == expected_per_cell
    assert c.CACHE_BUDGET_MB == expected_budget


def test_scale_readahead_shrinks_beyond_four_cells():
    from hyperwall.reliability import scale_readahead_s
    assert scale_readahead_s(1) == 60
    assert scale_readahead_s(4) == 60      # measured clean — unchanged
    assert scale_readahead_s(8) == 30      # burst size halves at 8 cells
    assert scale_readahead_s(16) == 15
    assert scale_readahead_s(100) == 10    # floor


def test_scale_bitrate_budget_graduated():
    from hyperwall.reliability import scale_bitrate_budget_mbps
    assert scale_bitrate_budget_mbps(4, 60) == 60   # <=4 cells: base wins
    assert scale_bitrate_budget_mbps(8, 60) == 50   # 800/(2*8) — relaxed 3x→2x
    assert scale_bitrate_budget_mbps(16, 60) == 25
    assert scale_bitrate_budget_mbps(100, 60) == 8  # floor


def test_scale_bitrate_budget_tracks_link_speed():
    # The per-file cap divides the usable link across cells with 2x burst
    # headroom — so a faster link raises the cap (up to the base) and a
    # slower one lowers it. Guards the greg→skyhawk 1 GbE default (800).
    from hyperwall.reliability import scale_bitrate_budget_mbps
    assert scale_bitrate_budget_mbps(8, 60, link_mbps=800) == 50   # 1 GbE
    assert scale_bitrate_budget_mbps(8, 60, link_mbps=2000) == 60  # 2.5 GbE → base
    assert scale_bitrate_budget_mbps(8, 60, link_mbps=400) == 25   # half-link


def test_link_mbps_constant_wired_into_effective_budget():
    # LINK_MBPS is the single knob; effective_bitrate_budget_mbps must feed it
    # through (default 800 → 50 Mbps at 8 cells).
    import os
    from hyperwall import constants as c
    assert c.LINK_MBPS == 800
    old = os.environ.pop("HYPERWALL_MAX_DIRECT_BITRATE_MBPS", None)
    try:
        assert c.effective_bitrate_budget_mbps(8) == 50
    finally:
        if old is not None:
            os.environ["HYPERWALL_MAX_DIRECT_BITRATE_MBPS"] = old


def test_effective_budget_env_override_wins(monkey=None):
    import os
    from hyperwall import constants as c
    old = os.environ.pop("HYPERWALL_MAX_DIRECT_BITRATE_MBPS", None)
    try:
        assert c.effective_bitrate_budget_mbps(8) == 50
        os.environ["HYPERWALL_MAX_DIRECT_BITRATE_MBPS"] = "60"
        # explicit env wins verbatim even at 8 cells (module constant was
        # parsed at import; presence of the var is the override signal)
        assert c.effective_bitrate_budget_mbps(8) == c.MAX_DIRECT_BITRATE_MBPS
    finally:
        os.environ.pop("HYPERWALL_MAX_DIRECT_BITRATE_MBPS", None)
        if old is not None:
            os.environ["HYPERWALL_MAX_DIRECT_BITRATE_MBPS"] = old


def test_gate_auto_transcode_caps_concurrency():
    from hyperwall.reliability import gate_auto_transcode
    # Under the ceiling → allowed; at/over → deferred to direct-play.
    assert gate_auto_transcode(True, active_transcodes=0, max_concurrent=4)
    assert gate_auto_transcode(True, active_transcodes=3, max_concurrent=4)
    assert not gate_auto_transcode(True, active_transcodes=4, max_concurrent=4)
    assert not gate_auto_transcode(True, active_transcodes=7, max_concurrent=4)


def test_gate_auto_transcode_passthrough_and_disable():
    from hyperwall.reliability import gate_auto_transcode
    # A clip that doesn't want transcode never transcodes, gate or not.
    assert not gate_auto_transcode(False, active_transcodes=0, max_concurrent=4)
    # max_concurrent <= 0 disables the gate entirely.
    assert gate_auto_transcode(True, active_transcodes=99, max_concurrent=0)


def test_prefetch_transcode_is_not_counted_as_active_playback():
    from hyperwall.reliability import active_transcode_count

    streams = [
        ("https://emby/Videos/a/stream?static=true", False),
        ("https://emby/Videos/b/master.m3u8?PlaySessionId=b", True),
        ("https://emby/Videos/c/master.m3u8?PlaySessionId=c", False),
    ]
    assert active_transcode_count(streams) == 1


def test_prefetch_transcode_detection_requires_hls_playlist():
    from hyperwall.reliability import is_transcode_stream

    assert is_transcode_stream("https://emby/Videos/a/master.m3u8?x=1")
    assert not is_transcode_stream("https://emby/Videos/a/stream.m3u8?static=true")
    assert not is_transcode_stream("https://emby/Videos/a/stream?static=true")


def test_active_transcode_count_ignores_stale_or_empty_streams():
    from hyperwall.reliability import active_transcode_count

    assert active_transcode_count(
        [("", False), (None, False), ("/Videos/a/master.m3u8", False)]
    ) == 1


def test_transcode_prefetch_is_disabled_when_server_budget_is_exhausted():
    from hyperwall.reliability import allow_transcode_prefetch

    assert allow_transcode_prefetch(active_transcodes=0, max_concurrent=4)
    assert not allow_transcode_prefetch(active_transcodes=4, max_concurrent=4)
    assert allow_transcode_prefetch(active_transcodes=4, max_concurrent=0)


def test_transcode_prefetch_counts_pending_transcodes_against_budget():
    from hyperwall.reliability import allow_transcode_prefetch

    assert not allow_transcode_prefetch(
        active_transcodes=3,
        pending_transcodes=1,
        max_concurrent=4,
    )
    assert allow_transcode_prefetch(
        active_transcodes=3,
        pending_transcodes=0,
        max_concurrent=4,
    )


def test_transcode_load_count_includes_pending_hls_streams():
    from hyperwall.reliability import transcode_load_count

    streams = [
        (f"/Videos/active-{i}/master.m3u8", False)
        for i in range(3)
    ] + [
        (f"/Videos/pending-{i}/master.m3u8", True)
        for i in range(5)
    ]
    assert transcode_load_count(streams) == 8


def test_max_concurrent_transcodes_constant():
    from hyperwall import constants as c
    assert c.MAX_CONCURRENT_TRANSCODES == 4


def test_macos_16gb_uses_conservative_cache_defaults():
    from hyperwall.constants import cache_defaults_for_platform
    assert cache_defaults_for_platform("darwin", 16 * 1024) == (256, 2_048)
    assert cache_defaults_for_platform("darwin", 32 * 1024) == (1_024, 8_192)
    assert cache_defaults_for_platform("linux", 16 * 1024) == (1_024, 8_192)


def test_macos_8_cell_cache_is_256_mib_with_bounded_readahead():
    from hyperwall.constants import apply_cache_budget, mpv_opts_for_platform
    opts = apply_cache_budget(
        mpv_opts_for_platform("darwin"), 8,
        platform="darwin", physical_memory_mb=16 * 1024,
    )
    assert opts["demuxer_max_bytes"] == "256MiB"
    assert opts["demuxer_readahead_secs"] == 30
    assert opts["cache_secs"] == 30


def test_apply_cache_budget_shape():
    from hyperwall.constants import apply_cache_budget
    out = apply_cache_budget({"demuxer_max_bytes": "512MiB", "vo": "gpu-next"}, 36)
    assert out["vo"] == "gpu-next"           # untouched keys preserved
    assert out["demuxer_max_bytes"].endswith("MiB")
    assert int(out["demuxer_max_bytes"][:-3]) < 512  # scaled down for 36 cells


def test_apply_cache_budget_scales_readahead():
    from hyperwall.constants import apply_cache_budget
    base = {"demuxer_max_bytes": "1024MiB", "demuxer_readahead_secs": 60,
            "cache_secs": 60}
    four = apply_cache_budget(dict(base), 4)
    eight = apply_cache_budget(dict(base), 8)
    assert four["demuxer_readahead_secs"] == 60   # unchanged at 4 cells
    assert eight["demuxer_readahead_secs"] == 30  # burst-aware at 8
    assert eight["cache_secs"] == 30              # follows readahead


def run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    sys.exit(run_all())
