"""Failure, publication, and repeat-run contracts for the NAS finalizer."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location(
    "rendition_finalizer_test_target", ROOT / "scripts/finalize-emby-renditions.py",
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def probe():
    return {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "120.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
             "width": 1920, "height": 1080, "avg_frame_rate": "60/1", "r_frame_rate": "60/1",
             "side_data_list": [{"rotation": -90}]},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2},
        ],
    }


class FakeMedia:
    timeout_s = 60

    def __init__(self, root, work):
        self.root, self.work = root, work
        self.input_probe = probe()
        self.output_probe = probe()
        self.remux_calls = 0
        self.probe_calls = []
        self.fail = False
        self.change_source = False

    def probe(self, path, timeout_s):
        self.probe_calls.append(path)
        return deepcopy(self.output_probe if path.startswith("/work/") else self.input_probe)

    def remux(self, source, destination, timeout_s):
        self.remux_calls += 1
        temp = self.work / PurePosixPath(destination).name
        temp.write_bytes(b"rewritten media payload" * 20)
        if self.change_source:
            (self.root / "alternate.mp4").write_bytes(b"new incoming copy" * 20)
        if self.fail:
            raise module.FinalizeError("ffmpeg_failed")


@contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory).resolve()
        root, work = base / "media", base / "work"
        root.mkdir()
        work.mkdir()
        source = root / "alternate.mp4"
        original = b"untouched original alternate" * 20
        source.write_bytes(original)
        now = time.time()
        os.utime(source, (now - 120, now - 120))
        media = FakeMedia(root, work)
        finalizer = module.Finalizer(root, PurePosixPath("/media"), work,
                                     PurePosixPath("/work"), media, min_free_bytes=0)
        # Filesystem publication is real; only privileged ownership/tool work
        # and directory fsync (unsupported on Windows CI) are replaced.
        with patch.object(module.os, "chown"), patch.object(module, "fsync_directory"):
            yield SimpleNamespace(root=root, work=work, source=source, original=original,
                                  now=now, media=media, finalizer=finalizer)


def test_receipt_is_identical_to_client_contract_and_valid_free_box():
    from hyperwall.renditions import NORMALIZATION_RECEIPT
    receipt = module.NORMALIZATION_RECEIPT
    assert receipt == NORMALIZATION_RECEIPT
    assert len(receipt) == 64
    assert struct.unpack(">I4s", receipt[:8]) == (64, b"free")


def test_stable_files_are_finalized_once_and_receipt_survives_restart():
    with fixture() as f:
        assert f.finalizer.scan(wall_time=f.now, monotonic=0)["waiting"] == 1
        assert f.media.remux_calls == 0
        assert f.finalizer.scan(wall_time=f.now + 59, monotonic=59)["waiting"] == 1
        result = f.finalizer.scan(wall_time=f.now + 60, monotonic=60)
        assert result["finalized"] == result["ready"] == 1
        assert module.has_receipt(f.source)
        assert f.finalizer.scan(wall_time=f.now + 61, monotonic=61)["ready"] == 1
        restarted = module.Finalizer(f.root, PurePosixPath("/media"), f.work,
                                     PurePosixPath("/work"), f.media, min_free_bytes=0)
        assert restarted.scan(wall_time=f.now + 62, monotonic=62)["ready"] == 1
        assert f.media.remux_calls == 1
        assert list(f.work.glob("*.partial.mp4")) == []


def test_publication_is_atomic_and_occurs_after_validation_and_receipt():
    with fixture() as f:
        replace = os.replace
        replacements = []

        def checked_replace(source, target):
            if target == f.source:
                assert f.source.read_bytes() == f.original
                assert module.has_receipt(source)
                assert len(f.media.probe_calls) == 2
                replacements.append((source, target))
            return replace(source, target)

        with patch.object(module.os, "replace", checked_replace):
            f.finalizer.finalize(f.source, module.Identity.read(f.source))
        assert len(replacements) == 1
        assert f.source.read_bytes() != f.original


def test_failed_remux_never_changes_alternate_and_is_not_repeated():
    with fixture() as f:
        f.media.fail = True
        f.finalizer.scan(wall_time=f.now, monotonic=0)
        assert f.finalizer.scan(wall_time=f.now + 60, monotonic=60)["failed"] == 1
        assert f.source.read_bytes() == f.original
        assert not module.has_receipt(f.source)
        assert f.finalizer.scan(wall_time=f.now + 120, monotonic=120)["failed"] == 1
        assert f.media.remux_calls == 1
        assert list(f.work.glob("*.partial.mp4")) == []


def test_invalid_output_duration_or_rotation_leaves_alternate_untouched():
    for defect in ("duration", "rotation"):
        with fixture() as f:
            if defect == "duration":
                f.media.output_probe["format"]["duration"] = "60"
            else:
                f.media.output_probe["streams"][0]["side_data_list"][0]["rotation"] = 0
            try:
                f.finalizer.finalize(f.source, module.Identity.read(f.source))
            except module.FinalizeError as exc:
                assert str(exc) == defect + "_changed"
            else:
                raise AssertionError("invalid remux was published")
            assert f.source.read_bytes() == f.original
            assert list(f.work.glob("*.partial.mp4")) == []


def test_incoming_copy_changed_during_remux_is_not_replaced():
    with fixture() as f:
        f.media.change_source = True
        try:
            f.finalizer.finalize(f.source, module.Identity.read(f.source))
        except module.FinalizeError as exc:
            assert str(exc) == "source_changed"
        else:
            raise AssertionError("concurrently written alternate was replaced")
        assert f.source.read_bytes() == b"new incoming copy" * 20
        assert not module.has_receipt(f.source)
        assert list(f.work.glob("*.partial.mp4")) == []


def test_profile_rejects_10bit_oversized_high_fps_and_incompatible_audio():
    for stream_index, key, value, expected in (
        (0, "pix_fmt", "yuv420p10le", "video_pixel_format"),
        (0, "width", 3840, "video_dimensions"),
        (0, "avg_frame_rate", "120/1", "video_fps"),
        (1, "channels", 6, "audio_format"),
        (1, "codec_name", "ac3", "audio_format"),
    ):
        data = probe()
        data["streams"][stream_index][key] = value
        try:
            module.validate_probe(data)
        except module.FinalizeError as exc:
            assert str(exc) == expected
        else:
            raise AssertionError("incompatible input accepted")
    data = probe()
    data["streams"][0]["width"], data["streams"][0]["height"] = 1080, 1920
    data["streams"] = data["streams"][:1]
    assert module.validate_probe(data)["audio"] is False


def test_low_disk_stops_before_media_tools_run():
    with fixture() as f:
        with patch.object(module.shutil, "disk_usage", return_value=SimpleNamespace(free=1)):
            try:
                f.finalizer.finalize(f.source, module.Identity.read(f.source))
            except module.LowDisk:
                pass
            else:
                raise AssertionError("insufficient reserve accepted")
        assert f.media.remux_calls == 0
        assert f.media.probe_calls == []
        assert f.source.read_bytes() == f.original


def test_symlink_inputs_and_directory_escapes_are_rejected():
    with fixture() as f:
        outside = f.work / "outside.mp4"
        outside.write_bytes(f.original)
        link = f.root / "link.mp4"
        try:
            link.symlink_to(outside)
        except OSError:
            if os.name == "nt":
                return  # Windows CI can lack the symlink privilege.
            raise
        try:
            module.safe_file(link, f.root)
        except module.FinalizeError as exc:
            assert str(exc) == "symlink_path"
        else:
            raise AssertionError("symlink input accepted")
        assert outside.read_bytes() == f.original


def test_remux_command_copies_selected_streams_and_preserves_metadata():
    media = module.DockerMedia("Emby")
    calls = []
    media.run = lambda *args: calls.append(args)
    media.remux("/media/input.mp4", "/work/output.mp4", 120)
    tool, args, timeout = calls[0]
    assert tool == "ffmpeg" and timeout == 120
    assert args[args.index("-c") + 1] == "copy"
    assert [args[i + 1] for i, arg in enumerate(args) if arg == "-map"] == ["0:v:0", "0:a:0?"]
    assert args[args.index("-map_metadata") + 1] == "0"
    assert args[args.index("-movflags") + 1] == "+faststart"
    assert args[args.index("-max_interleave_delta") + 1] == "1000000"
    assert "-noautorotate" in args


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc!r}")
    print(f"  {len(tests) - failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(run_all())
