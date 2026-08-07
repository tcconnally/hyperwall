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
    start = source.index("    def _start_audio_arm")
    end = source.index("\n    def _sync_mute_ui", start)
    body = source[start:end]
    assert "self._play_pos" in body
    assert "self._mpv.time_pos" not in body


def test_audio_arm_is_deferred_out_of_gui_handler():
    source = _source("hyperwall/cell.py")
    start = source.index("    def _enable_audio_track(self)")
    end = source.index("\n    def _sync_mute_ui", start)
    body = source[start:end]
    assert "_start_audio_arm" in body
    assert "self._mpv[\"aid\"] = \"auto\"" not in body


def test_recreated_mpv_defers_audio_until_after_load():
    source = _source("hyperwall/cell.py")
    start = source.index("    def _ensure_mpv")
    end = source.index("\n    def _stop_mpv_for_render_release", start)
    body = source[start:end]
    assert 'm["aid"] = "no"' in body
    assert 'self._audio_started = False' in body


def test_gui_seek_serializes_and_cancels_audio_relock():
    source = _source("hyperwall/cell.py")
    start = source.index("    def _seek_release")
    end = source.index("\n    def set_paused_ui", start)
    body = source[start:end]
    assert "_audio_arm_call_lock.acquire(blocking=False)" in body
    assert "QTimer.singleShot(50, self._seek_release)" in body
    assert "self._cancel_audio_arm(timeout_s=0.0)" in body
    assert body.index("self._cancel_audio_arm(timeout_s=0.0)") < body.index(
        "self._mpv.seek"
    )


def test_audio_arm_transition_serializes_replacement():
    source = _source("hyperwall/cell.py")
    start = source.index("    def play(")
    end = source.index("\n    def _play_impl", start)
    body = source[start:end]
    assert "_audio_arm_call_lock.acquire(blocking=False)" in body
    assert "_defer_play_until_audio_idle" in body

    start = source.index("    def _audio_arm_worker")
    end = source.index("\n    def _enable_audio_track_sync", start)
    body = source[start:end]
    assert "with self._audio_arm_call_lock" in body
    assert "_audio_arm_is_current" in body

    start = source.index("    def advance_to_prefetched")
    end = source.index("\n    def _advance_to_prefetched_impl", start)
    body = source[start:end]
    assert "_audio_arm_call_lock.acquire(blocking=False)" in body
    assert "return False" in body


def test_non_macos_audio_arm_preserves_sync_path():
    source = _source("hyperwall/cell.py")
    start = source.index("    def _enable_audio_track(self)")
    end = source.index("\n    def _sync_mute_ui", start)
    body = source[start:end]
    assert 'sys.platform != "darwin"' in body
    assert "_enable_audio_track_sync" in body


def test_stats_dump_reports_budgeted_mpv_options():
    source = _source("hyperwall/wall.py")
    assert "self._mpv_opts_effective = dict(budgeted)" in source
    start = source.index("    def _dump_stats_json")
    end = source.index("\n    # ── shutdown", start)
    body = source[start:end]
    assert '"mpv_opts_effective": dict(self._mpv_opts_effective)' in body


def test_render_release_stops_vo_before_context_free():
    source = _source("hyperwall/cell.py")
    start = source.index("    def _destroy_mpv_impl")
    end = source.index("\n    def _flush_stats", start)
    body = source[start:end]
    assert "_stop_mpv_for_render_release()" in body
    assert body.index("_stop_mpv_for_render_release()") < body.index(
        "self.video_frame.release()"
    )


def test_wall_shutdown_stops_vo_before_gl_release():
    source = _source("hyperwall/wall.py")
    start = source.index("    def _cleanup")
    end = source.index("\n        # Hide all windows immediately", start)
    body = source[start:end]
    assert "c._stop_mpv_for_render_release()" in body
    assert "with c._audio_arm_call_lock" in body
    assert body.index("c._stop_mpv_for_render_release()") < body.index(
        "c.video_frame.release()"
    )


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


def test_decoder_faults_have_per_cell_software_fallback_path():
    reliability = _source("hyperwall/reliability.py")
    cell = _source("hyperwall/cell.py")
    assert "classify_playback_fault" in reliability
    assert "decoder_recovery_plan" in reliability
    assert "_sig_decoder_fault" in cell
    assert "_handle_decoder_fault" in cell
    assert "_force_software_decode" in cell
    assert '"hwdec"] = "no"' in cell or '"hwdec": "no"' in cell


def test_decoder_fallback_state_is_considered_before_hwdec_option():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def _hardware_decode_enabled")
    end = cell.index("\n    def _handle_decoder_fault", start)
    body = cell[start:end]
    assert "if self._force_software_decode" in body
    assert "return False" in body


def test_reused_item_resets_fault_state_when_session_url_changes():
    cell = _source("hyperwall/cell.py")
    assert "preserve_failure_state" in cell
    assert "same_resource =" in cell
    assert "self._stream_url == url" in cell


def test_transport_faults_quarantine_failed_resource_after_bounded_retry():
    reliability = _source("hyperwall/reliability.py")
    cell = _source("hyperwall/cell.py")
    assert "transport_recovery_plan" in reliability
    assert "_sig_transport_fault" in cell
    assert "_handle_transport_fault" in cell
    assert "_transport_retry_count" in cell
    assert "_transport_resource_quarantined" in cell
    assert "_request_next_throttled(False)" in cell


def test_macos_mute_native_write_is_deferred_from_gui_handler():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def _apply_mute")
    end = cell.index("\n    @traced(\"cell._toggle_mute\")", start)
    body = cell[start:end]
    assert "_queue_mute_native" in body
    darwin = body[body.index('if sys.platform == "darwin":'):]
    assert "_queue_mute_native(muted)" in darwin


def test_shutdown_stops_qt_timers_on_gui_thread_before_pool_release():
    cell = _source("hyperwall/cell.py")
    wall = _source("hyperwall/wall.py")
    assert "def prepare_shutdown" in cell
    assert "_stop_qt_timers" in cell
    assert "c.prepare_shutdown()" in wall
    release = cell[cell.index("    def release"):]
    assert "_watchdog_timer.stop()" not in release


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
