"""Emby item metadata checks for the Linux disconnect benchmark.

This module is network-free. It keeps the benchmark wrapper's selection policy
separate from authentication and process launching so the wall-safe contract is
unit-testable without a live Emby server.
"""
from __future__ import annotations

import math
from typing import Any


_MAX_WIDTH = 1920
_MAX_HEIGHT = 1080
_MAX_FPS = 30.0
_MAX_VIDEO_BITRATE = 10_000_000


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _frame_rate(value: Any) -> float | None:
    if isinstance(value, str) and "/" in value:
        numerator, denominator = value.split("/", 1)
        numerator_value = _number(numerator)
        denominator_value = _number(denominator)
        if numerator_value is None or denominator_value in (None, 0):
            return None
        return numerator_value / denominator_value
    return _number(value)


def _media_source(item: dict[str, Any]) -> dict[str, Any]:
    sources = item.get("MediaSources")
    if not isinstance(sources, list) or not sources:
        return {}
    source = sources[0]
    return source if isinstance(source, dict) else {}


def _streams(item: dict[str, Any], source: dict[str, Any]) -> list[dict[str, Any]]:
    streams = source.get("MediaStreams") or item.get("MediaStreams") or []
    if not isinstance(streams, list):
        return []
    return [stream for stream in streams if isinstance(stream, dict)]


def wall_safe_violations(item: dict[str, Any]) -> list[str]:
    """Return violations of the verified normalized-library contract.

    The contract matches ``NormalizationProfile``: H.264 video, at most
    1920x1080, at most 30 fps, at most 10 Mbps video, and AAC audio with no
    more than two channels when an audio stream exists. Missing metadata is a
    violation so item selection fails closed instead of guessing.
    """
    source = _media_source(item)
    streams = _streams(item, source)
    videos = [stream for stream in streams if stream.get("Type") == "Video"]
    audios = [stream for stream in streams if stream.get("Type") == "Audio"]
    violations: list[str] = []

    if not videos:
        return ["missing_video"]
    video = videos[0]

    if str(video.get("Codec", "")).lower() != "h264":
        violations.append("video_codec")

    width = _number(video.get("Width"))
    height = _number(video.get("Height"))
    if (
        width is None
        or height is None
        or width < 2
        or height < 2
        or width > _MAX_WIDTH
        or height > _MAX_HEIGHT
    ):
        violations.append("video_dimensions")

    fps = _frame_rate(video.get("AverageFrameRate") or video.get("RealFrameRate"))
    if fps is None or fps > _MAX_FPS + 1e-6:
        violations.append("video_fps")

    bitrate = _number(video.get("BitRate") or source.get("Bitrate"))
    if bitrate is None:
        violations.append("video_bitrate_missing")
    elif bitrate > _MAX_VIDEO_BITRATE:
        violations.append("video_bitrate")

    if audios:
        audio = audios[0]
        if str(audio.get("Codec", "")).lower() != "aac":
            violations.append("audio_codec")
        channels = _number(audio.get("Channels"))
        if channels is None or channels > 2:
            violations.append("audio_channels")

    return violations


__all__ = ["wall_safe_violations"]
