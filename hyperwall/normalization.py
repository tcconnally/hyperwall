"""Wall-safe media normalization primitives.

The eight-cell production path is intentionally boring at runtime: every file
in the wall library is converted once, verified, and then direct-played. This
module contains no Emby or Qt dependencies so the conversion contract can be
run on the media host and tested in this repository.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Iterable


_MEDIA_EXTENSIONS = frozenset({
    ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg",
    ".mpg", ".ts", ".webm", ".wmv",
})


@dataclass(frozen=True)
class NormalizationProfile:
    """Output contract for one wall-safe media representation."""

    max_width: int = 1920
    max_height: int = 1080
    max_fps: float = 30.0
    max_video_bitrate_mbps: float = 10.0
    video_bitrate_mbps: float = 8.0
    video_codec: str = "libx264"
    video_preset: str = "medium"
    video_pixel_format: str = "yuv420p"
    audio_codec: str = "aac"
    audio_bitrate_kbps: int = 160
    audio_channels: int = 2
    audio_sample_rate: int = 48_000

    def __post_init__(self) -> None:
        if self.max_width < 2 or self.max_height < 2:
            raise ValueError("normalization dimensions must be at least 2")
        if self.max_fps <= 0:
            raise ValueError("max_fps must be positive")
        if not 0 < self.video_bitrate_mbps <= self.max_video_bitrate_mbps:
            raise ValueError("video bitrate must be positive and within max bitrate")
        if self.audio_channels < 1 or self.audio_sample_rate < 1:
            raise ValueError("audio settings must be positive")


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


def _streams(probe: dict[str, Any], codec_type: str) -> list[dict[str, Any]]:
    raw = probe.get("streams", [])
    if not isinstance(raw, list):
        return []
    return [
        stream for stream in raw
        if isinstance(stream, dict) and stream.get("codec_type") == codec_type
    ]


def validate_probe(
    probe: dict[str, Any], profile: NormalizationProfile,
) -> list[str]:
    """Return all wall-safety violations in an ffprobe JSON object.

    The list is intentionally stable and machine-readable. An empty list means
    the file already satisfies the post-normalization contract; it does not
    mean an arbitrary source should bypass normalization.
    """
    errors: list[str] = []
    videos = _streams(probe, "video")
    if not videos:
        return ["missing_video"]
    video = videos[0]

    if not (
        video.get("codec_name") == profile.video_codec
        or (profile.video_codec == "libx264" and video.get("codec_name") == "h264")
    ):
        errors.append("video_codec")
    width = _number(video.get("width"))
    height = _number(video.get("height"))
    if (
        width is None or height is None
        or width < 2 or height < 2
        or width > profile.max_width or height > profile.max_height
    ):
        errors.append("video_dimensions")
    fps = _frame_rate(video.get("avg_frame_rate"))
    if fps is None or fps > profile.max_fps + 1e-6:
        errors.append("video_fps")
    bitrate = _number(video.get("bit_rate"))
    if bitrate is not None and bitrate > profile.max_video_bitrate_mbps * 1_000_000:
        errors.append("video_bitrate")

    audios = _streams(probe, "audio")
    if audios:
        audio = audios[0]
        if audio.get("codec_name") != profile.audio_codec:
            errors.append("audio_codec")
        channels = _number(audio.get("channels"))
        if channels is None or channels > profile.audio_channels:
            errors.append("audio_channels")
    return errors


def normalized_output_path(
    input_path: Path, source_root: Path, destination_root: Path,
) -> Path:
    """Map one source file into a separate normalized library tree."""
    source = input_path.expanduser().resolve()
    root = source_root.expanduser().resolve()
    destination = destination_root.expanduser().resolve()
    try:
        relative = source.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"input is outside source root: {source}") from exc
    if source == root:
        raise ValueError("source root is not a media file")
    return destination / relative.with_suffix(".mp4")


def ffmpeg_args(
    input_path: Path, output_path: Path, profile: NormalizationProfile,
) -> list[str]:
    """Build a deterministic FFmpeg command for one normalized output."""
    vf = (
        f"fps={profile.max_fps:g},"
        f"scale=w='min(iw,{profile.max_width})':"
        f"h='min(ih,{profile.max_height})':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-y",
        "-i", str(input_path),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-vf", vf,
        "-fps_mode", "cfr",
        "-c:v", profile.video_codec,
        "-preset", profile.video_preset,
        "-pix_fmt", profile.video_pixel_format,
        "-b:v", f"{profile.video_bitrate_mbps:g}M",
        "-maxrate", f"{profile.max_video_bitrate_mbps:g}M",
        "-bufsize", f"{profile.max_video_bitrate_mbps * 2:g}M",
        "-profile:v", "high",
        "-level:v", "4.1",
        "-c:a", profile.audio_codec,
        "-b:a", f"{profile.audio_bitrate_kbps}k",
        "-ac", str(profile.audio_channels),
        "-ar", str(profile.audio_sample_rate),
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(output_path),
    ]


def ffprobe_args(input_path: Path) -> list[str]:
    """Build the ffprobe command used to verify a source or output."""
    return [
        "ffprobe", "-v", "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,avg_frame_rate,bit_rate,channels",
        "-of", "json",
        str(input_path),
    ]


def iter_media_files(source_root: Path) -> Iterable[Path]:
    """Yield ordinary media files in deterministic relative-path order."""
    root = source_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"source root is not a directory: {root}")
    paths = (
        path for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
        and path.suffix.lower() in _MEDIA_EXTENSIONS
    )
    yield from sorted(paths, key=lambda path: path.relative_to(root).as_posix().lower())


__all__ = [
    "NormalizationProfile",
    "ffmpeg_args",
    "ffprobe_args",
    "iter_media_files",
    "normalized_output_path",
    "validate_probe",
]
