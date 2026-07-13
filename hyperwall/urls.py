"""
Hyperwall — pure Emby URL construction + transcode heuristic (no PyQt / mpv).

Extracted so the genuinely bug-prone playback-URL logic is unit-testable
without a display server or a live Emby instance (Epic 3 / #8). `emby.py` and
`wall.py` delegate here.

⚠️ LOAD-BEARING: the DIRECT url MUST carry `static=true`. Emby 4.9.5.0's
`/Videos/{id}/stream` without `static=true` returns HTTP 500 for every item
(ffmpeg remux writes a temp file with no extension). `static=true` serves the
raw file. Do not remove it without live-curl proof against the target instance.
The transcode path uses `master.m3u8` (HLS) which is a different, working code
path on the server.
"""

from __future__ import annotations

from typing import Any


def _video_stream(item: dict[str, Any]) -> dict[str, Any]:
    """Primary video stream dict, tolerant of Emby's two shapes
    (MediaSources[0].MediaStreams or a top-level MediaStreams)."""
    src = (item.get("MediaSources") or [{}])[0]
    streams = src.get("MediaStreams") or item.get("MediaStreams") or []
    return next((s for s in streams if s.get("Type") == "Video"), {}) or {}


def exceeds_1080p(item: dict[str, Any]) -> bool:
    """True if the item's primary video stream carries more than 1080p worth
    of pixels, orientation-agnostic: long edge > 1920 or short edge > 1080.

    A naive `w > 1920 or h > 1080` flags every portrait 1080x1920 video —
    the same pixel count as landscape 1080p — forcing a pointless server
    transcode for each one. Tolerant of missing/None dimensions."""
    v = _video_stream(item)
    w = v.get("Width") or 0
    h = v.get("Height") or 0
    long_edge, short_edge = max(w, h), min(w, h)
    return long_edge > 1920 or short_edge > 1080


def exceeds_direct_budget(
    item: dict[str, Any],
    *,
    max_fps: float = 0,
    max_bitrate_mbps: float = 0,
) -> bool:
    """True if the stream is too heavy for direct play on a multi-cell wall.

    Resolution isn't the only cost driver: a 1080p 120fps 96 Mbps file plays
    DIRECT under the resolution-only heuristic and hammers decode + network
    across the grid. A limit of 0 disables that check (so env overrides can
    turn either off). Bitrate falls back from the video stream to the source
    container; fps prefers AverageFrameRate over RealFrameRate.
    """
    v = _video_stream(item)
    if max_fps > 0:
        fps = v.get("AverageFrameRate") or v.get("RealFrameRate") or 0
        if fps > max_fps:
            return True
    if max_bitrate_mbps > 0:
        src = (item.get("MediaSources") or [{}])[0]
        bitrate = v.get("BitRate") or src.get("Bitrate") or 0
        if bitrate > max_bitrate_mbps * 1_000_000:
            return True
    return False


def needs_transcode(
    item: dict[str, Any],
    *,
    auto_transcode: bool = True,
    max_fps: float = 0,
    max_bitrate_mbps: float = 0,
) -> bool:
    """Whether the auto-transcode heuristic wants a server-side downscale.

    `auto_transcode` is the resolved HYPERWALL_AUTO_TRANSCODE flag; when False
    the heuristic is disabled and everything tries DIRECT first. When on,
    a source transcodes only if it exceeds the optional fps/bitrate
    direct-play budget (see exceeds_direct_budget).

    Resolution is deliberately NOT a gate. The >1080p pixel check sent ~30%
    of plays to the server for live transcode, and the 2026-07-13 A/B bench
    showed that arm is where the pain lived: unseekable growing-HLS streams,
    mid-stream corruption, and wall-wide stalls when the server fell behind —
    while the direct arm played the same 4K sources with zero dropped frames.
    A desktop GPU decodes 4K trivially; concurrent live transcodes are the
    scarce resource. exceeds_1080p() remains for callers that want a
    resolution probe.
    """
    if not auto_transcode:
        return False
    return exceeds_direct_budget(
        item, max_fps=max_fps, max_bitrate_mbps=max_bitrate_mbps,
    )


def build_stream_url(
    *,
    base: str,
    item_id: str,
    api_key: str,
    session_id: str,
    transcode: bool,
    static: bool = True,
) -> str:
    """Build the media stream URL for an item.

    transcode=True  → HLS master playlist (server-side transcode to 1080p h264).
    transcode=False → DIRECT raw file. When `static` is True (the Emby default,
                      load-bearing — see module docstring) the url carries
                      static=true; a backend that must not use it can pass
                      static=False.
    """
    if transcode:
        return (
            f"{base}/Videos/{item_id}/master.m3u8?api_key={api_key}"
            f"&VideoCodec=h264&AudioCodec=aac&MaxAudioChannels=2"
            f"&MaxHeight=1080&MaxWidth=1920"
            f"&MaxFramerate=30&VideoBitrate=12000000"
            f"&PlaySessionId={session_id}"
        )
    direct = f"{base}/Videos/{item_id}/stream?api_key={api_key}"
    if static:
        direct += "&static=true"
    return direct
