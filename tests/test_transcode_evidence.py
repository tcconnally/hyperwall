"""Failure-only conversion evidence using production methods and fake native I/O.

No QApplication, playback, network requests or server jobs are created here.
The real recovery policies run against attributed resource contexts and signals.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
import random
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hyperwall.playback_state import PlaybackEvent
from hyperwall.reliability import (
    apply_jitter, classify_playback_fault, decoder_recovery_plan,
    escalation_plan, is_malformed_stream_fault, is_stalled,
    outage_recovery_plan, starvation_fault_reached, transport_recovery_plan,
)


class Signal:
    def __init__(self, *types):
        self.types = types
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


def make_cell(*, hardware=False, outage=False):
    path = ROOT / "hyperwall/cell.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "VideoCell")
    methods = {
        "_handle_prefetch_fault", "_handle_decoder_fault", "_handle_transport_fault",
        "_notify_resource_quarantined", "_notify_transcode_candidate", "_on_error",
        "_handle_buffering", "_check_stall", "_mpv_log",
    }
    cls.bases, cls.decorator_list = [], []
    cls.body = [node for node in cls.body if (
        isinstance(node, ast.FunctionDef) and node.name in methods
    ) or (
        isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "transcode_candidate"
            for target in node.targets
        )
    )]
    assert {node.name for node in cls.body if isinstance(node, ast.FunctionDef)} == methods
    for node in cls.body:
        if isinstance(node, ast.FunctionDef):
            node.decorator_list = []
    clock = SimpleNamespace(now=1000.0)
    timers = []
    namespace = {
        "pyqtSignal": Signal, "logger": logging.getLogger("transcode-evidence-test"),
        "_time": SimpleNamespace(monotonic=lambda: clock.now), "random": random,
        "PlaybackEvent": PlaybackEvent, "DECODER_FAULT_MAX": 2,
        "TRANSPORT_RETRY_MAX": 1, "MAX_RETRIES": 3,
        "STARVATION_FAULT_EVENTS": 3, "STARVATION_FAULT_TOTAL_S": 20,
        "CRASH_LOOP_COOLDOWN_S": 60, "OUTAGE_BACKOFF_S": 10,
        "STALL_TIMEOUT_S": 20, "MPV_LOG_NOISE": (),
        "QTimer": SimpleNamespace(singleShot=lambda *args: timers.append(args)),
    }
    for function in (
        apply_jitter, classify_playback_fault, decoder_recovery_plan, escalation_plan,
        is_malformed_stream_fault, is_stalled, outage_recovery_plan,
        starvation_fault_reached, transport_recovery_plan,
    ):
        namespace[function.__name__] = function
    tree.body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls]
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), namespace)
    cell = namespace["VideoCell"]()
    assert cell.transcode_candidate.types == (object, str)
    cell.transcode_candidate, cell.resource_quarantined = Signal(), Signal()
    cell.controller = SimpleNamespace(
        register_failure=Mock(return_value=outage), in_outage=Mock(return_value=outage),
    )
    cell._closing = False
    cell._mpv = object()
    cell._mpv_gen, cell._track_generation = 1, 2
    cell.current_item = {"Id": "active", "Name": "synthetic fixture"}
    cell.context = (1, 2, "active", "fixture://active", "fixture-session")
    cell._native_context_is_current = lambda context: context == cell.context
    cell._current_playback_token = lambda: SimpleNamespace(stream_url="fixture://active")
    cell._current_playback_state_identity = lambda: cell.context
    cell._playback_state_identity = lambda context: context
    cell._playback_controller = SimpleNamespace(transition=Mock())
    cell._stats_lock = threading.Lock()
    cell._hardware_decode_enabled = lambda: hardware and not cell._force_software_decode
    cell._force_software_decode = cell._force_transcode = False
    cell._decoder_fault_count = cell._decoder_recovery_exhausted = cell._decoder_quarantines = 0
    cell._decoder_software_fallbacks = 0
    cell._decoder_recovery_scheduled = cell._resource_quarantined = False
    cell._decoder_recovery_token = cell._playback_plan = None
    cell._transport_retry_count = 0
    cell._transport_recovery_scheduled = cell._transport_resource_quarantined = False
    cell._prefetch_advance_inflight = cell._prefetched = None
    cell._track_done = cell._parked = cell._paused = cell._dragging = False
    cell._retry_count = 0
    cell._record_failure_and_maybe_park = lambda: False
    cell._request_next_throttled = Mock()
    cell._show_title_overlay = Mock()
    cell._show_loading = Mock()
    cell._hide_overlay = Mock()
    cell.drop_prefetch = Mock(side_effect=lambda **_kw: setattr(cell, "_prefetched", None))
    cell._played_anything = True
    cell._freeze_t0 = cell._freeze_total_s = cell._last_seek_ts = 0.0
    cell._freeze_count = cell._freeze_postseek_count = cell._starvation_track_events = 0
    cell._starvation_track_total_s = 0.0
    cell._starvation_fault_scheduled = cell._buffering_card = False
    cell._cache_buffering_state = False
    cell._last_progress_ts = clock.now
    return cell, clock, timers


def test_malformed_prefetch_reports_queued_item_once_and_keeps_quarantine():
    cell, _clock, _timers = make_cell()
    item = {"Id": "queued", "Name": "queued fixture"}
    cell._prefetched = (item, "fixture://queued", "queued-session")
    context = (1, 3, "queued", "fixture://queued", "queued-session")
    cell._handle_prefetch_fault(context, "moov atom not found")
    cell._handle_prefetch_fault(context, "moov atom not found")
    assert cell.transcode_candidate.calls == [(item, "malformed_stream")]
    assert cell.resource_quarantined.calls == [(item,)]
    cell.drop_prefetch.assert_called_once_with(requeue=False)
    assert cell.current_item["Id"] == "active"


def test_prefetch_requires_malformed_bytes_and_exact_live_identity():
    for text, context, closing, advancing in (
        ("connection timed out", (1, 3, "queued", "fixture://queued", "session"), False, None),
        ("moov atom not found", (0, 3, "queued", "fixture://queued", "session"), False, None),
        ("moov atom not found", (1, 3, "queued", "fixture://queued", "session"), True, None),
        ("moov atom not found", (1, 3, "queued", "fixture://queued", "session"), False, 1),
    ):
        cell, _clock, _timers = make_cell()
        cell._prefetched = ({"Id": "queued"}, "fixture://queued", "session")
        cell._closing, cell._prefetch_advance_inflight = closing, advancing
        cell._handle_prefetch_fault(context, text)
        assert not cell.transcode_candidate.calls
        assert not cell.resource_quarantined.calls
        cell.drop_prefetch.assert_not_called()


def test_hardware_failure_requires_exhausted_software_recovery_before_candidate():
    cell, _clock, timers = make_cell(hardware=True)
    cell._handle_decoder_fault(cell.context, "hardware accelerator failed")
    assert cell._force_software_decode and len(timers) == 1
    assert not cell.transcode_candidate.calls
    cell._decoder_recovery_scheduled = False  # completed software reload
    cell._handle_decoder_fault(cell.context, "error while decoding")
    cell._handle_decoder_fault(cell.context, "error while decoding")
    assert cell.transcode_candidate.calls == [(cell.current_item, "decoder_recovery_exhausted")]
    assert cell.resource_quarantined.calls == [(cell.current_item,)]
    cell._request_next_throttled.assert_called_once_with(False)


def test_software_decoder_retry_is_not_automatically_a_conversion():
    cell, _clock, _timers = make_cell()
    cell._handle_decoder_fault(cell.context, "error while decoding")
    assert cell._decoder_recovery_scheduled
    assert not cell.transcode_candidate.calls


def test_malformed_active_stream_reports_specific_reason_and_rejects_stale_context():
    cell, _clock, _timers = make_cell()
    cell._handle_decoder_fault((0, *cell.context[1:]), "moov atom not found")
    assert not cell.transcode_candidate.calls
    cell._handle_decoder_fault(cell.context, "moov atom not found")
    assert cell.transcode_candidate.calls == [(cell.current_item, "malformed_stream")]
    assert cell.resource_quarantined.calls == [(cell.current_item,)]


def test_repeated_explicit_playback_failure_reports_only_transcode_transition():
    cell, _clock, _timers = make_cell()
    cell._on_error()
    assert not cell.transcode_candidate.calls
    cell._on_error()
    cell._on_error()
    assert cell.transcode_candidate.calls == [(cell.current_item, "playback_recovery_exhausted")]
    assert cell._force_transcode


def test_systemic_outage_never_reports_conversion_even_for_decoder_or_prefetch():
    cell, _clock, _timers = make_cell(outage=True)
    cell._on_error()
    cell._on_error()
    assert not cell._force_transcode
    cell._handle_decoder_fault(cell.context, "moov atom not found")
    cell._prefetched = ({"Id": "queued"}, "fixture://queued", "session")
    cell._handle_prefetch_fault((1, 3, "queued", "fixture://queued", "session"), "moov atom not found")
    assert not cell.transcode_candidate.calls
    assert len(cell.resource_quarantined.calls) == 2


def test_transport_recovery_exhaustion_does_not_report_conversion():
    cell, _clock, _timers = make_cell()
    cell._handle_transport_fault(cell.context, "connection timed out")
    cell._transport_recovery_scheduled = False
    cell._handle_transport_fault(cell.context, "connection timed out")
    assert cell._transport_resource_quarantined
    cell._on_error()  # later generic native error must not bypass quarantine
    assert not cell.transcode_candidate.calls


def test_cache_starvation_quarantine_remains_distinct_from_conversion_evidence():
    cell, clock, _timers = make_cell()
    for _ in range(3):
        cell._handle_buffering(cell.context, True)
        clock.now += 1
        cell._handle_buffering(cell.context, False)
    assert cell._resource_quarantined
    assert cell.resource_quarantined.calls == [(cell.current_item,)]
    assert not cell.transcode_candidate.calls


def test_watchdog_stalls_do_not_report_conversion_through_generic_escalation():
    cell, clock, _timers = make_cell()
    for _ in range(3):
        clock.now += 30
        cell._check_stall()
    assert cell._retry_count == 3 and not cell._force_transcode
    assert not cell.transcode_candidate.calls


def test_vo_drop_logs_and_extreme_metadata_do_not_report_conversion():
    cell, _clock, _timers = make_cell()
    cell.current_item.update({"Width": 7680, "Height": 4320, "Bitrate": 180_000_000})
    for message in ("Dropped 3609 frames", "VO frame dropped", "Video: 7680x4320 120fps"):
        cell._mpv_log("warn", "vo", message)
    assert not cell.transcode_candidate.calls


def test_missing_identity_shutdown_unknown_health_and_unknown_reason_fail_closed():
    cell, _clock, _timers = make_cell()
    cell._notify_transcode_candidate({}, "malformed_stream")
    cell._notify_transcode_candidate(cell.current_item, "high_bitrate")
    cell._closing = True
    cell._notify_transcode_candidate(cell.current_item, "malformed_stream")
    cell._closing = False
    cell.controller.in_outage.side_effect = RuntimeError("unknown wall health")
    cell._notify_transcode_candidate(cell.current_item, "malformed_stream")
    assert not cell.transcode_candidate.calls


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures} passed, {failures} failed out of {len(tests)} tests.")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
