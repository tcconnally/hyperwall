"""Regression contract for the macOS soak findings (2026-07-26).

The M5 soak measured a 210 ms mute/unmute slot and repeated 170–246 ms
next/prefetch slots. These tests pin the two low-risk corrections: relock from
our event-thread-maintained position cache (not synchronous libmpv property
IPC), and defer playlist prefetch until the current transition returns to Qt.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(relative: str) -> str:
    with open(os.path.join(_ROOT, relative), encoding="utf-8") as f:
        return f.read()


def test_audio_relock_uses_observed_position_cache():
    source = _source("hyperwall/cell.py")
    start = source.index("    def _enable_audio_track")
    end = source.index("\n    def _sync_mute_ui", start)
    body = source[start:end]
    assert "self._play_pos" in body
    assert "self._mpv.time_pos" not in body


def test_prefetch_is_deferred_after_transition():
    source = _source("hyperwall/wall.py")
    start = source.index("    def _arm_prefetch")
    end = source.index("\n    def run_on_main", start)
    body = source[start:end]
    assert "QTimer.singleShot(0," in body
    assert "def _queue" in body


def test_prefetch_hls_is_not_used_for_playback_concurrency_accounting():
    reliability = _source("hyperwall/reliability.py")
    wall = _source("hyperwall/wall.py")
    assert "transcode_load_count" in reliability
    assert "_transcode_load_count" in wall
    assert "allow_transcode_prefetch" in wall
    assert "_prefetched_stream_url" in _source("hyperwall/cell.py")

    start = wall.index("    def _transcode_load_count")
    end = wall.index("    def _build_url", start)
    accounting = wall[start:end]
    assert "_stream_url" in accounting
    assert "_prefetched_stream_url" in accounting
    assert "transcode_load_count(streams)" in accounting

    start = wall.index("    def _build_url")
    end = wall.index("\n    # ── session management", start)
    build_url = wall[start:end]
    assert "include_cell=prefetch" in build_url
    assert "occupied" in build_url


def test_prefetch_admission_does_not_demote_heavy_candidate_to_direct():
    wall = _source("hyperwall/wall.py")
    start = wall.index("    def _arm_prefetch")
    end = wall.index("\n    def run_on_main", start)
    body = wall[start:end]
    admission = body.index("if self._auto_transcode_requested(item)")
    build = body.index("url, sid = self._build_url", admission)
    assert admission < build
    assert body.index("self.playlists.push_front", admission) < build


def run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS  {test.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {test.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
