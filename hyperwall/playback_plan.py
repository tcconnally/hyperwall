from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class PlaybackPolicy:
    auto_transcode: bool = True
    max_fps: float = 66.0
    max_bitrate_mbps: float = 60.0


@dataclass(frozen=True)
class PlaybackPlan:
    item_id: str | None
    server_mode: str
    client_decoder: str
    requires_transcode_lease: bool
    reason: str
    source_fps: float | None
    source_bitrate_mbps: float | None

    def __post_init__(self) -> None:
        if self.server_mode not in {"direct", "server_transcode"}:
            raise ValueError("unknown server playback mode")
        if self.requires_transcode_lease != (self.server_mode == "server_transcode"):
            raise ValueError("transcode lease flag does not match server mode")


def _video_stream(item: dict[str, Any]) -> dict[str, Any]:
    sources = item.get("MediaSources") or []
    source = sources[0] if sources and isinstance(sources[0], dict) else {}
    streams = source.get("MediaStreams") or item.get("MediaStreams") or []
    return next((stream for stream in streams if isinstance(stream, dict) and stream.get("Type") == "Video"), {})


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _source_metrics(item: dict[str, Any]) -> tuple[float | None, float | None]:
    stream = _video_stream(item)
    sources = item.get("MediaSources") or []
    source = sources[0] if sources and isinstance(sources[0], dict) else {}
    fps = _finite_number(stream.get("AverageFrameRate") or stream.get("RealFrameRate"))
    bitrate = _finite_number(stream.get("BitRate") or source.get("Bitrate"))
    return fps, bitrate / 1_000_000 if bitrate is not None else None


def plan_playback(
    item: dict[str, Any],
    *,
    policy: PlaybackPolicy | None = None,
    client_decoder: str = "no",
) -> PlaybackPlan:
    resolved = policy or PlaybackPolicy()
    fps, bitrate_mbps = _source_metrics(item)
    decoder = str(client_decoder or "no").strip() or "no"
    over_fps = (
        resolved.max_fps > 0
        and fps is not None
        and fps > resolved.max_fps
    )
    over_bitrate = (
        resolved.max_bitrate_mbps > 0
        and bitrate_mbps is not None
        and bitrate_mbps > resolved.max_bitrate_mbps
    )
    if not resolved.auto_transcode:
        mode = "direct"
        reason = "auto_transcode_disabled"
    elif over_fps or over_bitrate:
        mode = "server_transcode"
        reason = (
            "fps_and_bitrate_over_budget"
            if over_fps and over_bitrate
            else "fps_over_budget"
            if over_fps
            else "bitrate_over_budget"
        )
    elif fps is None or bitrate_mbps is None:
        mode = "direct"
        reason = "missing_metadata"
    else:
        mode = "direct"
        reason = "within_direct_budget"
    return PlaybackPlan(
        item_id=item.get("Id"),
        server_mode=mode,
        client_decoder=decoder,
        requires_transcode_lease=mode == "server_transcode",
        reason=reason,
        source_fps=fps,
        source_bitrate_mbps=bitrate_mbps,
    )
