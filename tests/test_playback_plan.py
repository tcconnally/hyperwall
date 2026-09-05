# Contract tests for explicit playback planning and resource admission.
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperwall.playback_plan import (
    PlaybackPlan,
    PlaybackPolicy,
    filter_stable_direct_candidates,
    is_stable_direct_candidate,
    plan_playback,
    select_playback_candidates,
)
from hyperwall.resource_governor import ResourceGovernor
from hyperwall.urls import build_stream_url_for_plan


def _item(*, fps=None, bitrate=None, item_id="item-1"):
    video = {"Type": "Video"}
    if fps is not None:
        video["AverageFrameRate"] = fps
    if bitrate is not None:
        video["BitRate"] = bitrate
    return {"Id": item_id, "MediaSources": [{"Bitrate": bitrate, "MediaStreams": [video]}]}


def test_direct_plan_keeps_server_and_client_modes_separate():
    plan = plan_playback(
        _item(fps=30, bitrate=20_000_000),
        policy=PlaybackPolicy(auto_transcode=True, max_fps=66, max_bitrate_mbps=50),
        client_decoder="no",
    )
    assert isinstance(plan, PlaybackPlan)
    assert plan.server_mode == "direct"
    assert plan.client_decoder == "no"
    assert plan.requires_transcode_lease is False
    assert plan.reason == "within_direct_budget"


def test_over_budget_plan_requires_server_transcode_lease():
    plan = plan_playback(
        _item(fps=30, bitrate=80_000_000),
        policy=PlaybackPolicy(auto_transcode=True, max_fps=66, max_bitrate_mbps=50),
        client_decoder="videotoolbox-copy",
    )
    assert plan.server_mode == "server_transcode"
    assert plan.client_decoder == "videotoolbox-copy"
    assert plan.requires_transcode_lease is True
    assert plan.reason == "bitrate_over_budget"


def test_auto_transcode_disabled_forces_direct_without_changing_client_decoder():
    plan = plan_playback(
        _item(fps=120, bitrate=120_000_000),
        policy=PlaybackPolicy(auto_transcode=False, max_fps=66, max_bitrate_mbps=50),
        client_decoder="no",
    )
    assert plan.server_mode == "direct"
    assert plan.client_decoder == "no"
    assert plan.requires_transcode_lease is False
    assert plan.reason == "auto_transcode_disabled"


def test_stable_direct_candidate_requires_complete_bounded_metadata():
    assert is_stable_direct_candidate(
        _item(fps=30, bitrate=20_000_000),
        max_fps=30,
        max_bitrate_mbps=20,
    ) is True
    assert is_stable_direct_candidate(
        _item(fps=60, bitrate=10_000_000),
        max_fps=30,
        max_bitrate_mbps=20,
    ) is False
    assert is_stable_direct_candidate(
        _item(fps=24, bitrate=40_000_000),
        max_fps=30,
        max_bitrate_mbps=20,
    ) is False
    assert is_stable_direct_candidate(
        _item(),
        max_fps=30,
        max_bitrate_mbps=20,
    ) is False


def test_stable_direct_pool_excludes_heavy_and_unmeasured_items():
    safe = _item(fps=30, bitrate=15_000_000, item_id="safe")
    high_fps = _item(fps=60, bitrate=10_000_000, item_id="high-fps")
    high_bitrate = _item(fps=24, bitrate=40_000_000, item_id="high-bitrate")
    unknown = _item(item_id="unknown")

    assert filter_stable_direct_candidates(
        [safe, high_fps, high_bitrate, unknown],
        max_fps=30,
        max_bitrate_mbps=20,
    ) == [safe]


def test_normal_playback_pool_retains_items_for_transcode_planning():
    items = [
        _item(fps=30, bitrate=15_000_000, item_id="safe"),
        _item(fps=60, bitrate=40_000_000, item_id="heavy"),
        _item(item_id="unknown"),
    ]

    assert select_playback_candidates(
        items,
        direct_only=False,
        max_fps=30,
        max_bitrate_mbps=20,
    ) == items
    assert select_playback_candidates(
        items,
        direct_only=True,
        max_fps=30,
        max_bitrate_mbps=20,
    ) == [items[0]]


def test_missing_source_metadata_uses_server_transcode_when_auto_enabled():
    plan = plan_playback(
        _item(),
        policy=PlaybackPolicy(auto_transcode=True, max_fps=66, max_bitrate_mbps=50),
        client_decoder="no",
    )
    assert plan.server_mode == "server_transcode"
    assert plan.requires_transcode_lease is True
    assert plan.reason == "missing_metadata_transcode"


def test_boolean_source_metadata_is_treated_as_missing():
    item = _item(fps=True, bitrate=True)
    policy = PlaybackPolicy(auto_transcode=True, max_fps=66, max_bitrate_mbps=50)
    plan = plan_playback(item, policy=policy, client_decoder="no")

    assert plan.server_mode == "server_transcode"
    assert plan.reason == "missing_metadata_transcode"
    assert is_stable_direct_candidate(
        item, max_fps=30, max_bitrate_mbps=20,
    ) is False



def test_client_decoder_fallback_updates_only_client_dimension():
    plan = plan_playback(_item(bitrate=80_000_000), client_decoder="videotoolbox-copy")
    fallback = plan.with_client_decoder("no")

    assert fallback.server_mode == plan.server_mode
    assert fallback.requires_transcode_lease == plan.requires_transcode_lease
    assert fallback.client_decoder == "no"
    assert fallback.reason == "client_decoder_fallback"


def test_plan_reports_both_over_budget_dimensions():
    plan = plan_playback(
        _item(fps=120, bitrate=80_000_000),
        policy=PlaybackPolicy(auto_transcode=True, max_fps=66, max_bitrate_mbps=50),
        client_decoder="no",
    )
    assert plan.server_mode == "server_transcode"
    assert plan.reason == "fps_and_bitrate_over_budget"
    assert plan.source_fps == 120
    assert plan.source_bitrate_mbps == 80.0


def test_missing_source_metadata_is_explicitly_planned():
    plan = plan_playback(
        _item(),
        policy=PlaybackPolicy(auto_transcode=True, max_fps=66, max_bitrate_mbps=50),
        client_decoder="no",
    )
    assert plan.server_mode == "server_transcode"
    assert plan.reason == "missing_metadata_transcode"
    assert plan.requires_transcode_lease is True
    assert plan.source_fps is None
    assert plan.source_bitrate_mbps is None


def test_url_builder_uses_server_mode_from_plan():
    policy = PlaybackPolicy(auto_transcode=True, max_fps=66, max_bitrate_mbps=50)
    direct = plan_playback(_item(fps=30, bitrate=20_000_000), policy=policy, client_decoder="no")
    transcode = plan_playback(_item(fps=30, bitrate=80_000_000), policy=policy, client_decoder="no")

    direct_url = build_stream_url_for_plan(
        base="http://emby:8096", item_id="direct", api_key="key",
        session_id="direct-session", plan=direct,
    )
    transcode_url = build_stream_url_for_plan(
        base="http://emby:8096", item_id="transcoded", api_key="key",
        session_id="transcoded-session", plan=transcode,
    )

    assert "/stream?" in direct_url
    assert "master.m3u8" not in direct_url
    assert "master.m3u8" in transcode_url
    assert "/stream?" not in transcode_url





def test_plan_carries_budget_context_without_session_credentials():
    policy = PlaybackPolicy(
        auto_transcode=True,
        max_fps=60.0,
        max_bitrate_mbps=40.0,
        cache_budget_mb=128,
        aggregate_cache_budget_mb=2048,
        readahead_seconds=12,
    )
    plan = plan_playback(_item(bitrate=20_000_000), policy=policy, client_decoder="no")
    payload = plan.as_dict()

    assert payload["cache_budget_mb"] == 128
    assert payload["aggregate_cache_budget_mb"] == 2048
    assert payload["readahead_seconds"] == 12
    assert "session_id" not in payload
    assert "api_key" not in payload


def test_wall_controller_uses_explicit_plan_boundary():
    wall_source = Path(__file__).resolve().parents[1].joinpath("hyperwall", "wall.py").read_text()
    assert "from .playback_plan import" in wall_source
    assert "build_stream_url_for_plan" in wall_source
    assert "def _plan_for_item" in wall_source
    assert "def _build_playback_request" in wall_source
    assert "ResourceGovernor" in wall_source
    assert "_resource_governor" in wall_source


def test_wall_controller_wires_full_library_pool_and_explicit_direct_only_escape():
    wall_source = Path(__file__).resolve().parents[1].joinpath("hyperwall", "wall.py").read_text()
    assert "stable_direct_profile_for_platform" in wall_source
    assert "select_playback_candidates" in wall_source
    assert "Full-library playback profile" in wall_source


def test_resource_governor_limits_server_transcode_leases():
    governor = ResourceGovernor(max_server_transcodes=2)
    plan = plan_playback(
        _item(bitrate=80_000_000, item_id="transcoded"),
        policy=PlaybackPolicy(auto_transcode=True, max_fps=66, max_bitrate_mbps=50),
        client_decoder="no",
    )
    assert governor.acquire(plan, "session-a") is True
    assert governor.acquire(plan, "session-b") is True
    assert governor.acquire(plan, "session-c") is False
    assert governor.active_server_transcodes == 2
    assert governor.release("session-a") is True
    assert governor.acquire(plan, "session-c") is True


def test_resource_governor_is_idempotent_and_direct_plans_need_no_lease():
    governor = ResourceGovernor(max_server_transcodes=1)
    policy = PlaybackPolicy(auto_transcode=True, max_fps=66, max_bitrate_mbps=50)
    transcode_plan = plan_playback(_item(fps=30, bitrate=80_000_000), policy=policy, client_decoder="no")
    direct_plan = plan_playback(_item(fps=30, bitrate=20_000_000, item_id="direct"), policy=policy, client_decoder="no")
    assert governor.acquire(transcode_plan, "session-a") is True
    assert governor.acquire(transcode_plan, "session-a") is True
    assert governor.active_server_transcodes == 1
    assert governor.acquire(direct_plan, "session-direct") is True
    assert governor.active_server_transcodes == 1
    assert governor.release("unknown") is False
    assert governor.release("session-a") is True
    assert governor.active_server_transcodes == 0


def run_all() -> int:
    failures = 0
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {fn.__name__}: {exc}")
    print(f"\\n{len(tests) - failures} passed, {failures} failed")
    return failures


if __name__ == "__main__":
    raise SystemExit(run_all())
