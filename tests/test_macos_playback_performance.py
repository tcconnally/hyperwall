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
    assert "lambda token=token: self._seek_release(token)" in body
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
    assert "_release_render_context_on_gui()" in body
    helper_start = source.index("    def _release_render_context_on_gui")
    helper_end = source.index("\n    def _destroy_mpv", helper_start)
    helper = source[helper_start:helper_end]
    assert helper.index("_stop_mpv_for_render_release()") < helper.index(
        "self.video_frame.release()"
    )


def test_wall_shutdown_stops_vo_before_gl_release():
    source = _source("hyperwall/wall.py")
    wall = source
    start = source.index("    def _cleanup")
    end = source.index("\n        # Hide all windows immediately", start)
    body = source[start:end]
    assert "c._release_render_context_on_gui()" in body
    assert "shutdown_deadline" in body
    assert "timeout=min(2.0" in body
    assert "_session_registry" in wall


def test_async_prefetch_transition_blocks_stale_recovery_paths():
    cell = _source("hyperwall/cell.py")
    for name, end_marker in ((
        "_handle_prefetch_fault", "\n    def _handle_decoder_fault",
    ), (
        "_handle_decoder_fault", "\n    def _recover_current_decoder",
    ), (
        "_handle_transport_fault", "\n    def _retry_transport_resource",
    ), (
        "_check_stall", "\n    def _record_failure_and_maybe_park",
    )):
        start = cell.index(f"    def {name}(")
        end = cell.index(end_marker, start)
        assert "_prefetch_advance_inflight" in cell[start:end], name


def test_macos_prefetched_advance_is_queued_off_gui_thread():
    source = _source("hyperwall/cell.py")
    start = source.index("    def advance_to_prefetched(")
    end = source.index("\n    def _advance_to_prefetched_impl", start)
    body = source[start:end]
    assert "_queue_prefetched_advance" in body
    assert "playlist-next" not in body
    assert "sys.platform == \"darwin\"" in body


def test_wall_does_not_rearm_prefetch_before_async_advance_finishes():
    source = _source("hyperwall/wall.py")
    start = source.index("    def next_video(")
    end = source.index("\n    def _on_resource_quarantined", start)
    body = source[start:end]
    fast = body[body.index("if cell.advance_to_prefetched()"):body.index("item = self.playlists.next", body.index("if cell.advance_to_prefetched()"))]
    assert "_arm_prefetch(cell)" not in fast
    assert "sync_broadcast_cell_update(cell)" not in fast


def test_prefetch_is_deferred_after_transition():
    source = _source("hyperwall/wall.py")
    start = source.index("    def _arm_prefetch")
    end = source.index("\n    def run_on_main", start)
    body = source[start:end]
    assert "QTimer.singleShot(0," in body
    assert "def _queue" in body


def test_prefetch_starts_are_globally_slot_paced():
    wall = _source("hyperwall/wall.py")
    constants = _source("hyperwall/constants.py")
    reliability = _source("hyperwall/reliability.py")
    assert "PREFETCH_MIN_INTERVAL_MS" in constants
    assert "prefetch_slot" in reliability
    start = wall.index("    def _arm_prefetch")
    end = wall.index("\n    def _do_prefetch", start)
    body = wall[start:end]
    assert "prefetch_slot" in body
    assert "_prefetch_next_ready_ts" in body
    assert "_do_prefetch_if_current" in body
    assert "QTimer.singleShot(0, _queue)" in body


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


def test_late_malformed_prefetch_log_tail_is_suppressed():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def _mpv_log(")
    end = cell.index("\n    # ── Qt events", start)
    log_body = cell[start:end]
    assert "_prefetch_fault_suppression_until" in log_body
    assert "is_malformed_stream_fault(text)" in log_body
    start = cell.index("    def _handle_prefetch_fault(")
    end = cell.index("\n    def _handle_decoder_fault", start)
    handler = cell[start:end]
    assert "_prefetch_fault_suppression_until" in handler
    assert "_time.monotonic()" in handler


def test_malformed_prefetch_logs_quarantine_queued_resource():
    cell = _source("hyperwall/cell.py")
    assert "context_for_prefetch_fault" in cell
    assert "_sig_prefetch_fault" in cell
    assert "_handle_prefetch_fault" in cell
    start = cell.index("    def _mpv_log(")
    end = cell.index("\n    # ── Qt events", start)
    body = cell[start:end]
    assert "context_for_prefetch_fault" in body
    assert "_sig_prefetch_fault.emit" in body
    start = cell.index("    def _handle_prefetch_fault(")
    end = cell.index("\n    def _handle_decoder_fault", start)
    body = cell[start:end]
    assert "resource_quarantined.emit" in body
    assert "drop_prefetch(requeue=False)" in body


def test_unscoped_mpv_logs_use_active_resource_context():
    reliability = _source("hyperwall/reliability.py")
    cell = _source("hyperwall/cell.py")
    assert "context_for_unscoped_fault" in reliability
    start = cell.index("    def _mpv_log(")
    end = cell.index("\n    # ── Qt events", start)
    body = cell[start:end]
    assert "context_for_unscoped_fault" in body
    assert "self._native_active_context" in body
    assert "self._switching" in body


def test_decoder_handler_passes_malformed_stream_state_to_plan():
    cell = _source("hyperwall/cell.py")
    reliability = _source("hyperwall/reliability.py")
    assert "is_malformed_stream_fault" in reliability
    start = cell.index("    def _handle_decoder_fault(")
    end = cell.index("\n    def _recover_current_decoder", start)
    body = cell[start:end]
    assert "is_malformed_stream_fault(_message)" in body
    assert "malformed_stream=" in body


def test_decoder_fallback_state_is_considered_before_hwdec_option():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def _hardware_decode_enabled")
    end = cell.index("\n    def _handle_decoder_fault", start)
    body = cell[start:end]
    assert "if self._force_software_decode" in body
    assert "return False" in body


def test_fault_callbacks_are_track_scoped_and_duplicate_safe():
    cell = _source("hyperwall/cell.py")
    assert "pyqtSignal(object, str)" in cell
    assert "playback_token_is_current" in cell
    decoder = cell[cell.index("    def _handle_decoder_fault"):]
    assert "_decoder_recovery_scheduled" in decoder
    assert decoder.index("_decoder_recovery_scheduled") < decoder.index(
        "self._decoder_fault_count += 1"
    )
    assert "_recover_current_decoder(token)" in decoder
    assert "_retry_transport_resource(token)" in decoder


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
    assert "drop_prefetch" in cell[cell.index("def _handle_transport_fault"):]
    assert "playlist-remove" in cell


def test_native_callbacks_bind_active_event_track_context():
    cell = _source("hyperwall/cell.py")
    assert "NativeContext" in cell
    assert "_native_track_observers" in cell
    assert "_bind_native_track_observers" in cell
    assert "_unbind_native_track_observers" in cell
    observers = cell[cell.index("    def _bind_native_track_observers"):]
    assert "context: NativeContext" in observers
    assert "property_observer" in observers
    assert "_native_context_is_current(context)" in observers
    log = cell[cell.index("        def _log_handler"):cell.index("        if sys.platform", cell.index("        def _log_handler"))]
    assert "_native_active_context" not in log



def test_delayed_recovery_and_backoff_callbacks_are_token_bound():
    cell = _source("hyperwall/cell.py")
    assert "_decoder_recovery_token" in cell
    assert "_transport_recovery_token" in cell
    assert "_retry_backoff_token" in cell
    assert "lambda token=token" in cell
    assert "if self._decoder_recovery_token == token" in cell
    assert "if self._transport_recovery_token == token" in cell


def test_all_gui_native_controls_use_shared_call_ownership():
    cell = _source("hyperwall/cell.py")
    for method in (
        "_seek_press",
        "_toggle_play",
        "_toggle_loop",
        "_flush_stats",
        "drop_prefetch",
    ):
        start = cell.index(f"    def {method}")
        end = cell.find("\n    def ", start + 1)
        body = cell[start:] if end < 0 else cell[start:end]
        assert (
            "_audio_arm_call_lock" in body
            or "_native_call" in body
            or "_remove_prefetched_playlist_entry" in body
        ), method


def test_shutdown_drains_audio_before_gui_render_release():
    wall = _source("hyperwall/wall.py")
    cleanup = wall[wall.index("    def _cleanup"):]
    assert "_cancel_audio_arm(" in cleanup
    assert "shutdown_deadline" in cleanup
    assert "_release_render_context_on_gui" in cleanup
    assert "Skipping bounded GL pre-release" not in cleanup


def test_session_stop_retries_and_keeps_registry_until_success():
    wall = _source("hyperwall/wall.py")
    start = wall.index("    def stop_emby_session")
    end = wall.index("\n    def _submit_api", start)
    body = wall[start:end]
    assert "_session_stop_inflight" in body
    assert "status_code < 300" in body
    assert "_session_registry.pop" in body
    assert "except Exception" in body


def test_windows_callback_contracts_are_generation_aware():
    freeze = _source("tests/test_freeze_visibility.py")
    audit = _source("tests/test_audit_regressions.py")
    assert "_handle_buffering(" in freeze
    assert "_context(cell" in freeze
    assert "_handle_track_done((cell._mpv_gen, cell._track_generation" in audit


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
    cleanup = wall[wall.index("    def _cleanup") :]
    assert "shutdown_deadline" in cleanup
    assert "timeout=min(2.0" in cleanup
    release = cell[cell.index("    def release"):]
    assert "_watchdog_timer.stop()" not in release


def test_same_platform_memory_probe_honors_explicit_budget():
    constants = _source("hyperwall/constants.py")
    assert "physical_memory_mb is None" in constants


def test_callback_activation_is_bound_to_current_mpv_generation():
    cell = _source("hyperwall/cell.py")
    start = cell.index('        @m.event_callback("start-file")')
    end = cell.index('\n        @m.event_callback("end-file")', start)
    body = cell[start:end]
    assert "gen != self._mpv_gen" in body
    assert "_pending_native_context" in body
    assert "self._native_event_track_generation = self._track_generation" not in body


def test_prefetch_decline_requeues_reserved_item():
    wall = _source("hyperwall/wall.py")
    start = wall.index("    def _arm_prefetch")
    end = wall.index("\n    def run_on_main", start)
    body = wall[start:end]
    declined = body[body.index("if not cell.prefetch"):]
    assert "self.playlists.push_front" in declined
    assert "item" in declined


def test_prefetched_metadata_survives_failed_playlist_advance():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def _advance_to_prefetched_impl")
    end = cell.index("\n    def _stop_qt_timers", start)
    body = cell[start:end]
    assert body.index('self._mpv.command("playlist-next")') < body.index(
        "self._prefetched = None"
    )


def test_handoff_stops_old_session_only_after_play_admission():
    wall = _source("hyperwall/wall.py")
    start = wall.index("    def _hand_off")
    end = wall.index("\n    def _arm_prefetch", start)
    body = wall[start:end]
    assert "def _on_started(started: bool)" in body
    assert "on_started=_on_started" in body
    assert body.index("def _on_started") < body.index(
        "stop_emby_session(old_item_id, old_session_id)"
    )


def test_shutdown_tracks_native_release_failures_on_every_platform():
    cell = _source("hyperwall/cell.py")
    wall = _source("hyperwall/wall.py")
    destroy = cell[cell.index("    def _destroy_mpv("):cell.index("    def _destroy_mpv_impl", cell.index("    def _destroy_mpv("))]
    cleanup = wall[wall.index("    def _cleanup"):]
    assert "_destroy_retry" in destroy or "request_render_release_when_idle" in destroy
    assert "_native_finalizer_records" in cell
    assert "has_pending_native_finalizer" in cell
    assert "deferred_render_cells" in cleanup
    assert "_finish_deferred_shutdown" in wall


def test_audio_workers_reject_shutdown_before_touching_native_state():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def _start_audio_arm")
    end = cell.index("\n    def _audio_arm_worker", start)
    assert "self._closing" in cell[start:end]
    start = cell.index("    def _audio_arm_worker")
    end = cell.index("\n    def _queue_mute_native", start)
    body = cell[start:end]
    assert "not self._closing" in body or "self._closing" in body


def test_native_control_retries_preserve_latest_control_token():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def _queue_native_property")
    end = cell.index("\n    def _write_mute_native", start)
    body = cell[start:end]
    assert "_native_control_is_current" in body
    assert "token" in body
    assert "track_generation" in body


def test_stopped_session_tombstones_are_bounded():
    wall = _source("hyperwall/wall.py")
    assert "deque(maxlen=" in wall or "_stopped_session_ids.clear" in wall



def test_native_signals_carry_immutable_resource_context():
    cell = _source("hyperwall/cell.py")
    assert "NativeContext = tuple" in cell
    assert "_sig_eof = pyqtSignal(object, str)" in cell
    assert "_sig_track_done = pyqtSignal(object)" in cell
    assert "_native_context_is_current(context)" in cell


def test_prefetch_drop_retains_metadata_until_native_removal():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def drop_prefetch")
    end = cell.index('    @traced("cell.advance_to_prefetched")', start)
    body = cell[start:end]
    assert "if not removed" in body
    assert "return False" in body
    assert body.index("if not removed") < body.index("_forget_prefetch_after_native_clear")


def test_deferred_play_supersession_notifies_replaced_callback():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def _defer_play_until_audio_idle")
    end = cell.index('    @traced("cell.play")', start)
    body = cell[start:end]
    assert "superseded" in body
    assert "previous[3](False)" in body
    assert "previous[0] is not item" in body


def test_native_call_revalidates_under_lock():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def _native_call")
    end = cell.index("    def _stop_mpv_for_render_release", start)
    body = cell[start:end]
    assert "valid" in body
    assert "if valid is not None and not valid()" in body
    loop_start = cell.index("    def _loop_current_track")
    loop_end = cell.index('    @traced("cell._handle_track_done")', loop_start)
    loop = cell[loop_start:loop_end]
    assert "valid=lambda: self._playback_token_is_current(token)" in loop


def test_native_retry_paths_have_deadlines():
    cell = _source("hyperwall/cell.py")
    assert "_shutdown_render_release_deadline" in cell
    assert "_destroy_retry_deadline" in cell
    assert "Bounded native destroy deadline reached" in cell


def test_prefetched_handoff_stops_previous_session():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def _advance_to_prefetched_impl")
    end = cell.index("    def _stop_qt_timers", start)
    body = cell[start:end]
    assert "old_session_id = self._emby_session_id" in body
    assert "self.controller.stop_emby_session(old_item_id, old_session_id)" in body
    assert body.index("self._emby_session_id = sid") < body.index(
        "stop_emby_session(old_item_id, old_session_id)"
    )


def test_session_registry_is_bounded_and_evictions_cleanup():
    wall = _source("hyperwall/wall.py")
    assert "_session_registry_limit" in wall
    assert "_session_cleanup_ledger" in wall
    assert "_retain_session_cleanup_locked" in wall
    start = wall.index("    def _register_session")
    end = wall.index("    def stop_emby_session", start)
    body = wall[start:end]
    assert "while len(self._session_registry) > limit" in body
    assert "self.stop_emby_session(old_item, old_session)" in body


def test_delayed_native_retries_are_playback_token_bound():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def _native_call")
    end = cell.index("    def _stop_mpv_for_render_release", start)
    body = cell[start:end]
    assert "operation_token = self._current_playback_token()" in body
    assert "_playback_token_is_current(operation_token)" in body
    assert "valid is not None and not valid()" in body
    seek = cell[cell.index('    @traced("cell._seek_release")'):cell.index("    def set_paused_ui")]
    assert "lambda token=token: self._seek_release(token)" in seek
    audio = cell[cell.index("    def _enable_audio_track_sync("):cell.index("    def _enable_audio_track(self)")]
    assert "self._closing" in audio
    assert "lambda token=token: self._enable_audio_track_sync(token)" in audio


def test_native_context_session_identity_is_validated():
    cell = _source("hyperwall/cell.py")
    start = cell.index("    def _native_context_is_current")
    end = cell.index("    def _native_call", start)
    body = cell[start:end]
    assert "context[4]" in body
    assert "session_matches" in body
    assert "pending = self._pending_native_context" in body


def test_start_file_binds_native_playlist_entry_identity():
    cell = _source("hyperwall/cell.py")
    assert "_native_playlist_entry_id" in cell
    assert "_native_playlist_contexts.get(entry_id)" in cell
    assert "context[1] != self._track_generation" in cell
    assert "_native_playlist_contexts[entry_id] = context" in cell


def test_end_file_errors_reach_bound_recovery_handlers():
    cell = _source("hyperwall/cell.py")
    start = cell.index('        @m.event_callback("end-file")')
    end = cell.index("        self._mpv = m", start)
    body = cell[start:end]
    assert "_native_context_for_event" in body
    assert "_sig_decoder_fault.emit(context" in body
    assert "_sig_transport_fault.emit(context" in body


def test_cleanup_capacity_does_not_evict_unresolved_records():
    wall = _source("hyperwall/wall.py")
    assert "Session cleanup capacity exhausted" in wall
    assert "pop(oldest" not in wall
    assert "Session admission closed" in wall


def test_global_shutdown_retains_all_finalizer_types():
    cell = _source("hyperwall/cell.py")
    wall = _source("hyperwall/wall.py")
    assert "_render_finalizer_pending" in cell
    assert "_native_finalizer_records" in cell
    assert "shutdown_deadline" in cell[cell.index("    def release"):]
    finish = wall[wall.index("    def _finish_deferred_shutdown"):wall.index("    def _cleanup")]
    assert "has_pending_native_finalizer" in finish
    assert "has_pending_render_finalizer" in finish
    cleanup = wall[wall.index("    def _cleanup"):]
    assert "timeout_s=max(0.0, shutdown_deadline" in cleanup
    assert "Client close exceeded global shutdown deadline" in cleanup


def test_macos_render_teardown_keeps_ctypes_callback_alive_until_free():
    source = _source("hyperwall/macembed.py")
    release_start = source.index("    def release")
    free_start = source.index("    def _free_ctx", release_start)
    release = source[release_start:free_start]
    assert "self._accepting_frames = False" in release
    # python-mpv replaces/releases the CFUNCTYPE trampoline before calling
    # the libmpv setter; doing that while the vo thread is in the old callback
    # is a use-after-free. Keep the guarded callback installed through free().
    assert "self._ctx.update_cb = None" not in release

    free_end = source.index("\n    # ── GL plumbing", free_start)
    free = source[free_start:free_end]
    assert "ctx, self._ctx = self._ctx, None" in free
    assert free.index("ctx, self._ctx = self._ctx, None") < free.index(
        "ctx.free()"
    )
    assert "self._abandoned_contexts.append(ctx)" in free

    abandon_start = free.index("if QOpenGLContext.currentContext() is None:")
    abandon_end = free.index("\n        try:", abandon_start)
    abandoned = free[abandon_start:abandon_end]
    assert "self._abandoned_contexts.append(self._ctx)" in abandoned
    assert abandoned.index("self._abandoned_contexts.append(self._ctx)") < abandoned.index(
        "self._ctx = None"
    )

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
