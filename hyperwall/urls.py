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


def exceeds_1080p(item: dict[str, Any]) -> bool:
    """True if the item's primary video stream is wider than 1920 or taller
    than 1080. Tolerant of Emby's two shapes (MediaSources[0].MediaStreams or
    a top-level MediaStreams) and missing/None dimensions."""
    src = (item.get("MediaSources") or [{}])[0]
    streams = src.get("MediaStreams") or item.get("MediaStreams") or []
    v = next((s for s in streams if s.get("Type") == "Video"), {}) or {}
    w = v.get("Width") or 0
    h = v.get("Height") or 0
    return w > 1920 or h > 1080


def needs_transcode(item: dict[str, Any], *, auto_transcode: bool = True) -> bool:
    """Whether the auto-transcode heuristic wants a server-side downscale.

    `auto_transcode` is the resolved HYPERWALL_AUTO_TRANSCODE flag; when False
    the heuristic is disabled and everything tries DIRECT first.
    """
    if not auto_transcode:
        return False
    return exceeds_1080p(item)


def build_stream_url(
    *,
    base: str,
    item_id: str,
    api_key: str,
    session_id: str,
    transcode: bool,
) -> str:
    """Build the Emby stream URL for an item.

    transcode=True  → HLS master playlist (server-side transcode to 1080p h264).
    transcode=False → DIRECT raw file via static=true (load-bearing — see
                      module docstring).
    """
    if transcode:
        return (
            f"{base}/Videos/{item_id}/master.m3u8?api_key={api_key}"
            f"&VideoCodec=h264&AudioCodec=aac&MaxAudioChannels=2"
            f"&MaxHeight=1080&MaxWidth=1920"
            f"&MaxFramerate=30&VideoBitrate=12000000"
            f"&PlaySessionId={session_id}"
        )
    return f"{base}/Videos/{item_id}/stream?api_key={api_key}&static=true"
