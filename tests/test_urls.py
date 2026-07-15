"""
Unit tests for hyperwall.urls (Epic 3) — pure Emby URL + transcode logic.

No PyQt / mpv / Emby. Runnable anywhere Python is.
Run: python tests/test_urls.py

The critical assertion here is that the DIRECT path carries static=true — a
load-bearing workaround for Emby 4.9.5.0's HTTP 500 on /stream without it.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from hyperwall.urls import (  # noqa: E402
    build_stream_url,
    exceeds_1080p,
    exceeds_direct_budget,
    needs_transcode,
    tag_names,
)


def _item(width=None, height=None, top_level=False, fps=None, bitrate=None):
    stream = {"Type": "Video"}
    if width is not None:
        stream["Width"] = width
    if height is not None:
        stream["Height"] = height
    if fps is not None:
        stream["AverageFrameRate"] = fps
    if bitrate is not None:
        stream["BitRate"] = bitrate
    if top_level:
        return {"Id": "x", "MediaStreams": [stream]}
    return {"Id": "x", "MediaSources": [{"MediaStreams": [stream]}]}


# ── exceeds_1080p ─────────────────────────────────────────────────────────────

def test_1080p_source_does_not_exceed():
    assert not exceeds_1080p(_item(1920, 1080))


def test_4k_source_exceeds():
    assert exceeds_1080p(_item(3840, 2160))


def test_width_just_over_exceeds():
    assert exceeds_1080p(_item(1921, 1080))


def test_height_just_over_exceeds():
    assert exceeds_1080p(_item(1920, 1081))


def test_top_level_mediastreams_shape():
    assert exceeds_1080p(_item(3840, 2160, top_level=True))


def test_missing_dimensions_safe():
    # No Width/Height keys → treated as 0 → not exceeding, no crash.
    assert not exceeds_1080p({"Id": "x", "MediaSources": [{"MediaStreams": [{"Type": "Video"}]}]})


def test_none_dimensions_safe():
    assert not exceeds_1080p(_item(None, None))


def test_no_streams_safe():
    assert not exceeds_1080p({"Id": "x"})


def test_portrait_1080x1920_does_not_exceed():
    # Same pixel count as landscape 1080p — the old `h > 1080` check forced a
    # pointless server transcode for every vertical video.
    assert not exceeds_1080p(_item(1080, 1920))


def test_portrait_4k_exceeds():
    # 2160x3840 portrait is genuinely more than 1080p worth of pixels.
    assert exceeds_1080p(_item(2160, 3840))


def test_portrait_wide_short_edge_exceeds():
    # 1440x1920: long edge fits but short edge exceeds 1080.
    assert exceeds_1080p(_item(1440, 1920))


# ── exceeds_direct_budget (fps / bitrate) ─────────────────────────────────────

def test_budget_disabled_by_default():
    assert not exceeds_direct_budget(_item(1920, 1080, fps=120, bitrate=96_000_000))


def test_high_fps_exceeds_budget():
    assert exceeds_direct_budget(_item(1920, 1080, fps=120), max_fps=66)


def test_60fps_within_budget():
    assert not exceeds_direct_budget(_item(1920, 1080, fps=60), max_fps=66)


def test_high_bitrate_exceeds_budget():
    assert exceeds_direct_budget(
        _item(1920, 1080, bitrate=96_000_000), max_bitrate_mbps=60,
    )


def test_bitrate_falls_back_to_source_container():
    item = {
        "Id": "x",
        "MediaSources": [{
            "Bitrate": 96_000_000,
            "MediaStreams": [{"Type": "Video", "Width": 1920, "Height": 1080}],
        }],
    }
    assert exceeds_direct_budget(item, max_bitrate_mbps=60)


def test_budget_missing_fields_safe():
    assert not exceeds_direct_budget({"Id": "x"}, max_fps=66, max_bitrate_mbps=60)


def test_needs_transcode_includes_budget():
    heavy = _item(1920, 1080, fps=120)
    assert needs_transcode(heavy, auto_transcode=True, max_fps=66)
    assert not needs_transcode(heavy, auto_transcode=False, max_fps=66)


# ── needs_transcode (flag binding) ────────────────────────────────────────────

def test_needs_transcode_false_for_4k():
    # Resolution is not a gate (dropped 2026-07-13): 4K within the fps/bitrate
    # budget direct-plays — the A/B bench measured 0 drops on the direct arm
    # while server live-transcodes stalled, corrupted, and couldn't seek ahead.
    assert not needs_transcode(_item(3840, 2160), auto_transcode=True)


def test_needs_transcode_true_for_4k_over_budget():
    # The budget still catches genuinely heavy sources regardless of size.
    assert needs_transcode(
        _item(3840, 2160, fps=120), auto_transcode=True, max_fps=66,
    )


def test_needs_transcode_false_when_disabled():
    # Even a 4K source must not transcode when the flag is off.
    assert not needs_transcode(_item(3840, 2160), auto_transcode=False)


def test_needs_transcode_false_for_1080p():
    assert not needs_transcode(_item(1920, 1080), auto_transcode=True)


# ── build_stream_url ──────────────────────────────────────────────────────────

def test_direct_url_has_static_true():
    # LOAD-BEARING: Emby 4.9.5.0 returns 500 on /stream without static=true.
    url = build_stream_url(
        base="http://emby:8096", item_id="ID1", api_key="KEY",
        session_id="SID", transcode=False,
    )
    assert "static=true" in url, url
    assert "/Videos/ID1/stream?" in url
    assert "api_key=KEY" in url
    assert "master.m3u8" not in url


def test_direct_url_static_false_omits_param():
    # A backend that must not use static=true can opt out; default stays True.
    url = build_stream_url(
        base="http://h", item_id="I", api_key="K",
        session_id="S", transcode=False, static=False,
    )
    assert "static=true" not in url
    assert "/Videos/I/stream?" in url


def test_transcode_url_is_hls_master():
    url = build_stream_url(
        base="http://emby:8096", item_id="ID1", api_key="KEY",
        session_id="SID", transcode=True,
    )
    assert "/Videos/ID1/master.m3u8?" in url
    assert "static=true" not in url            # HLS path must NOT use static
    assert "VideoCodec=h264" in url
    assert "MaxHeight=1080" in url
    assert "MaxWidth=1920" in url
    assert "PlaySessionId=SID" in url


def test_urls_carry_item_and_key():
    for transcode in (True, False):
        url = build_stream_url(
            base="http://h", item_id="abc123", api_key="tok",
            session_id="s", transcode=transcode,
        )
        assert "abc123" in url
        assert "api_key=tok" in url


# ── tag_names (Emby TagItems vs Tags shape) ───────────────────────────────────

def test_tag_names_reads_tagitems_when_tags_null():
    # The exact shape greg's Emby returns for a tagged item (probed 2026-07-15):
    # Tags is null, the applied tag lives in TagItems. The old item["Tags"]
    # read saw this as untagged → wrong ToDelete indicator + list(None) crash.
    item = {"Id": "x", "Tags": None,
            "TagItems": [{"Name": "ToDelete", "Id": 21516}]}
    assert tag_names(item) == ["ToDelete"]


def test_tag_names_tagitems_takes_precedence_over_tags():
    item = {"TagItems": [{"Name": "ToDelete"}], "Tags": ["stale"]}
    assert tag_names(item) == ["ToDelete"]


def test_tag_names_falls_back_to_string_list():
    assert tag_names({"Tags": ["ToDelete", "keep"]}) == ["ToDelete", "keep"]


def test_tag_names_falls_back_to_dict_list_tags():
    assert tag_names({"Tags": [{"Name": "ToDelete"}]}) == ["ToDelete"]


def test_tag_names_empty_and_missing_safe():
    assert tag_names({}) == []
    assert tag_names({"Tags": None, "TagItems": None}) == []
    assert tag_names({"Tags": [], "TagItems": []}) == []


def test_tag_names_untagged_item_not_checked():
    # The bug's user-visible symptom: an untagged item must compute False, a
    # ToDelete-tagged one True — regardless of which field Emby populates.
    assert "ToDelete" not in tag_names({"Tags": None, "TagItems": []})
    assert "ToDelete" in tag_names({"TagItems": [{"Name": "ToDelete"}]})


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
