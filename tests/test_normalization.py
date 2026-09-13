"""Tests for the wall-safe media normalization contract."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperwall.normalization import (  # noqa: E402
    NormalizationProfile,
    ffmpeg_args,
    normalized_output_path,
    validate_probe,
)


def test_default_profile_is_safe_for_eight_cells():
    profile = NormalizationProfile()
    assert profile.max_width == 1920
    assert profile.max_height == 1080
    assert profile.max_fps == 30
    assert profile.max_video_bitrate_mbps == 10
    assert profile.video_codec == "libx264"
    assert profile.audio_codec == "aac"


def test_ffmpeg_command_is_cfr_h264_aac_and_faststart():
    profile = NormalizationProfile()
    args = ffmpeg_args(Path("source.mkv"), Path("out.mp4"), profile)
    joined = " ".join(args)
    assert args[:4] == ["ffmpeg", "-nostdin", "-hide_banner", "-y"]
    assert "-map 0:v:0" in joined
    assert "-map 0:a:0?" in joined
    assert "fps=30" in joined
    assert "force_original_aspect_ratio=decrease" in joined
    assert "-c:v libx264" in joined
    assert "-c:a aac" in joined
    assert "-ac 2" in joined
    assert "-b:a 160k" in joined
    assert "-maxrate 10M" in joined
    assert "-movflags +faststart" in joined


def test_output_path_preserves_relative_layout_but_changes_container():
    source_root = Path("/library").resolve()
    dest_root = Path("/wall-safe").resolve()
    result = normalized_output_path(
        source_root / "shows" / "episode.mkv", source_root, dest_root,
    )
    assert result == dest_root / "shows" / "episode.mp4"


def test_output_path_rejects_input_outside_source_root():
    source_root = Path("/library").resolve()
    dest_root = Path("/wall-safe").resolve()
    try:
        normalized_output_path(Path("/other/movie.mkv"), source_root, dest_root)
    except ValueError as exc:
        assert "source root" in str(exc)
    else:
        raise AssertionError("outside-source input was accepted")


def test_wall_safe_probe_requires_normalized_codecs_and_bounds():
    probe = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30/1",
                "bit_rate": "8000000",
            },
            {"codec_type": "audio", "codec_name": "aac", "channels": 2},
        ],
    }
    assert validate_probe(probe, NormalizationProfile()) == []


def test_wall_safe_probe_reports_every_violation():
    probe = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 3840,
                "height": 2160,
                "avg_frame_rate": "60/1",
                "bit_rate": "40000000",
            },
            {"codec_type": "audio", "codec_name": "opus", "channels": 6},
        ],
    }
    errors = validate_probe(probe, NormalizationProfile())
    assert {"video_codec", "video_dimensions", "video_fps", "video_bitrate", "audio_codec", "audio_channels"} <= set(errors)


def test_cli_dry_run_never_creates_destination():
    script = Path(__file__).resolve().parents[1] / "scripts" / "normalize-library.py"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source"
        destination = root / "destination"
        (source / "shows").mkdir(parents=True)
        (source / "shows" / "episode.mkv").write_bytes(b"fixture")
        result = subprocess.run(
            [
                sys.executable, str(script),
                "--source", str(source),
                "--destination", str(destination),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert '"status": "plan"' in result.stdout
        assert '"dry_run": true' in result.stdout
        assert not destination.exists()


def run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"  {len(tests) - failures} passed, {failures} failed")
    return failures


if __name__ == "__main__":
    raise SystemExit(run_all())
