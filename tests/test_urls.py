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
    needs_transcode,
)


def _item(width=None, height=None, top_level=False):
    stream = {"Type": "Video"}
    if width is not None:
        stream["Width"] = width
    if height is not None:
        stream["Height"] = height
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


# ── needs_transcode (flag binding) ────────────────────────────────────────────

def test_needs_transcode_true_for_4k_when_enabled():
    assert needs_transcode(_item(3840, 2160), auto_transcode=True)


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
