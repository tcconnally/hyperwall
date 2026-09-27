"""Local diagnostics stay bounded and never wait on playback/native locks."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_snapshot_skips_busy_native_stats_and_render_locks():
    from hyperwall.local_telemetry import cached_cell_snapshot
    from hyperwall.render_telemetry import RenderTelemetry
    lock = threading.Lock()
    render = RenderTelemetry()
    cell = SimpleNamespace(current_item={"Id": "17", "Name": "private title"},
                           muted=True, _stats_lock=lock,
                           video_frame=SimpleNamespace(_render_telemetry=render))
    lock.acquire()
    render._lock.acquire()
    try:
        started = time.monotonic()
        row = cached_cell_snapshot(cell, 0)
        assert time.monotonic() - started < .05
        assert row["stats_busy"] and row["render"] is None
        assert "private title" not in json.dumps(row)
    finally:
        render._lock.release()
        lock.release()


def test_snapshot_uses_cached_totals_and_preserves_render_counters():
    from hyperwall.local_telemetry import cached_cell_snapshot
    from hyperwall.render_telemetry import RenderTelemetry
    render = RenderTelemetry()
    render.record_frame_ready()
    cell = SimpleNamespace(current_item={"Id": "17", "_hyperwall_prepared": True},
                           muted=False, _stats_lock=threading.Lock(),
                           _stats_total={"frame-drop-count": 3},
                           _stats_current={"frame-drop-count": 2},
                           _stats_info={"hwdec-current": "nvdec", "url": "private-token"},
                           video_frame=SimpleNamespace(_render_telemetry=render))
    row = cached_cell_snapshot(cell, 2)
    assert row["counters"]["frame-drop-count"] == 5
    assert row["media"]["hwdec-current"] == "nvdec"
    assert row["render"]["frame_ready"] == 1
    assert render.snapshot()["total"]["frame_ready"] == 1
    assert "private-token" not in json.dumps(row)


def test_slow_storage_cannot_block_submit_or_grow_queue():
    from hyperwall.local_telemetry import LocalWriter
    entered, release = threading.Event(), threading.Event()
    def blocked_run(_self):
        entered.set()
        release.wait(3)
    with patch.object(LocalWriter, "_run", blocked_run):
        writer = LocalWriter(Path("/unused"))
        assert entered.wait(1)
        started = time.monotonic()
        try:
            for i in range(1000): writer.submit({"event": "sample", "n": i})
            writer.submit({"event": "escape", "windows_hidden_ms": 1})
            writer.stop()
            assert time.monotonic() - started < .1
            assert len(writer.pending) == 32 and writer.dropped == 969
            assert writer.pending[-1]["event"] == "escape"
        finally:
            release.set()
            writer.thread.join(1)


def test_jsonl_is_local_flushed_private_and_records_escape():
    from hyperwall.local_telemetry import LocalWriter
    with tempfile.TemporaryDirectory() as directory:
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            writer = LocalWriter(Path(directory) / "telemetry")
            writer.submit({"event": "sample", "cells": []})
            writer.submit({"event": "escape", "windows_hidden_ms": 2.5})
            writer.stop()
            writer.thread.join(2)
        assert not writer.thread.is_alive()
        rows = [json.loads(line) for line in writer.path.read_text().splitlines()]
        assert [row["event"] for row in rows] == ["sample", "escape"]
        assert "cpu_percent_one_core_100" in rows[0]["resources"]
        assert rows[1]["windows_hidden_ms"] == 2.5
        assert writer.path.stat().st_mode & 0o777 == 0o600


def test_full_library_launcher_platforms_without_starting_wall():
    import subprocess
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        launcher = base / "run-hyperwall.sh"
        launcher.write_text((root / launcher.name).read_text())
        fake = base / "launch.sh"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$HYPERWALL_HWDEC" "$HYPERWALL_PREPARED_ONLY" "$HYPERWALL_STABLE_DIRECT_ONLY" "$HYPERWALL_LOCAL_TELEMETRY" "$HYPERWALL_STATS" "$HYPERWALL_AUTO_TRANSCODE" "$HYPERWALL_TRANSCODE_ON_FAILURE"\n')
        fake.chmod(0o700)
        for platform, decoder in (("Darwin", "videotoolbox-copy"), ("Linux", "auto-safe")):
            uname = base / "uname"
            uname.write_text(f"#!/bin/sh\nprintf '%s\\n' '{platform}'\n")
            uname.chmod(0o700)
            env = {"PATH": f"{base}:/usr/bin:/bin", "HOME": directory}
            result = subprocess.run(["/bin/bash", str(launcher)], env=env,
                                    capture_output=True, text=True, check=True)
            assert result.stdout.splitlines() == [decoder, "0", "0", "1", "1", "0", "1"]


def run_all():
    try:
        import PyQt6
    except ImportError:
        print("SKIP local telemetry requires PyQt6")
        return 0
    failures = 0
    for name, fn in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:
                failures += 1
                print("FAIL", name, repr(exc))
    return failures


if __name__ == "__main__":
    raise SystemExit(run_all())
