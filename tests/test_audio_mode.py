"""Audio behavior with recording native calls, without Qt or live playback.

Compile the production VideoCell methods into a plain harness so every CI lane
can exercise them. Widgets, media, and native rendering are replaced; audio
control, worker ownership, load/advance, and buffering logic are not copied.
"""
from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hyperwall.constants import audio_mode_from_env, native_wid, uses_render_api
from hyperwall.playback_state import PlaybackEvent
from hyperwall.reliability import audio_track_for_mute, starvation_fault_reached
from hyperwall.urls import tag_names


class FakeMpv:
    def __init__(self, **_kwargs):
        self.props = {"aid": "no", "mute": True, "pause": False, "volume": 70.0}
        self.calls = []
        self.block_property = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def __getitem__(self, name):
        return self.props[name]

    def __setitem__(self, name, value):
        if name == self.block_property:
            self.entered.set()
            assert self.release.wait(2), "native test call was never released"
        self.props[name] = value
        self.calls.append((name, value))

    def seek(self, *args):
        self.calls.append(("seek", *args))

    def command(self, *args):
        self.calls.append(args)

    def event_callback(self, _name):
        return lambda fn: fn


def make_cell(mode="lazy", platform="win32"):
    path = Path(__file__).resolve().parents[1] / "hyperwall/cell.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    cell_node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "VideoCell")
    names = {
        "_continuous_audio_for_item", "_select_audio_for_load",
        "_ensure_mpv", "_begin_track", "_play_impl", "_async_play_worker",
        "_queue_async_play", "_async_play_is_current", "_finish_async_play",
        "_queue_prefetched_advance", "_prefetched_advance_is_current",
        "_prefetched_advance_worker", "_finish_prefetched_advance",
        "_advance_to_prefetched_impl", "_apply_mute", "_vol_changed",
        "_enable_audio_track", "_start_audio_arm", "_request_audio_track_state",
        "_audio_arm_worker", "_audio_arm_is_current", "_cancel_audio_arm",
        "_disable_audio_track", "_enable_audio_track_sync",
        "_enable_audio_track_sync_locked", "_disable_audio_track_sync",
        "_queue_mute_native", "_queue_native_property", "_native_property_worker",
        "_native_control_is_current", "_write_native_property_latest",
        "_write_mute_native", "_write_volume_native", "_handle_buffering",
    }
    methods = [n for n in cell_node.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in methods} == names
    for method in methods:
        method.decorator_list = []
    workers = []

    def new_thread(**kwargs):
        worker = threading.Thread(**kwargs)
        workers.append(worker)
        return worker

    namespace = {
        "_time": time, "os": os,
        "sys": SimpleNamespace(platform=platform, stdout=sys.stdout, stderr=sys.stderr),
        "threading": SimpleNamespace(Thread=new_thread),
        "logger": logging.getLogger("audio-test"),
        "uses_render_api": lambda: uses_render_api(platform),
        "audio_track_for_mute": audio_track_for_mute,
        "native_wid": lambda wid: native_wid(wid, platform),
        "MPV_OPTS": {"hwdec": "no"}, "apply_env_overrides": dict,
        "PlaybackEvent": PlaybackEvent, "tag_names": tag_names,
        "STATS_ENABLED": False, "_G_PAUSE": "pause",
        "STARVATION_FAULT_EVENTS": 3, "STARVATION_FAULT_TOTAL_S": 20,
        "starvation_fault_reached": starvation_fault_reached,
        "QTimer": SimpleNamespace(singleShot=lambda *_a: None),
    }
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *methods], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    harness = type("CellHarness", (), {name: namespace[name] for name in names})
    cell = harness()
    cell._audio_mode = mode
    cell._continuous_audio = mode == "continuous"
    cell._mpv = None
    cell._mpv_opts = {"hwdec": "no"}
    cell._mpv_gen = 0
    cell._closing = False
    cell._force_software_decode = False
    cell._force_transcode = False
    cell._audio_started = False
    cell.muted = True
    cell.looping = False
    cell._native_playlist_contexts = {}
    cell._audio_arm_lock = threading.Lock()
    cell._audio_arm_call_lock = threading.Lock()
    cell._audio_arm_token = 0
    cell._audio_arm_inflight_token = None
    cell._audio_arm_pending_enabled = None
    cell._audio_arm_done = threading.Event()
    cell._audio_arm_done.set()
    cell._native_control_serial = 0
    cell._native_control_tokens = {}
    cell._track_generation = 1
    cell._play_pos = 12.0
    cell._last_seek_ts = 0.0
    cell._parked = False
    cell._played_anything = True
    cell.current_item = {"Id": "initial", "Name": "synthetic fixture"}
    cell._stream_url = "fixture://initial"
    cell._playback_plan = None
    cell._prefetched_playback_plan = None
    cell._emby_session_id = "old-session"
    cell._emby_item_id = "initial"
    cell._prefetch_advance_serial = 0
    cell._prefetch_advance_inflight = None
    cell._async_play_serial = 0
    cell._async_play_inflight = None
    cell._async_play_pending = None
    cell._freeze_t0 = 0.0
    cell._freeze_count = 0
    cell._freeze_postseek_count = 0
    cell._freeze_total_s = 0.0
    cell._starvation_track_events = 0
    cell._starvation_track_total_s = 0.0
    cell._starvation_fault_scheduled = False
    cell._track_done = False
    cell._resource_quarantined = False
    cell._cache_buffering_state = False
    for name in ("video_frame", "btn_play", "btn_tag", "btn_fav", "lbl_title", "vol_slider", "controller", "_playback_controller", "_sig_prefetched_advance", "_sig_play_finished", "_sig_eof"):
        setattr(cell, name, Mock())
    cell.video_frame.isVisible.return_value = True
    cell.video_frame.winId.return_value = 123
    cell.vol_slider.isSliderDown.return_value = False
    cell.vol_slider.value.return_value = 70
    for name in ("_sync_mute_ui", "_invalidate_async_play", "_invalidate_prefetched_advance", "drop_prefetch", "_forget_prefetch_after_native_clear", "_hide_overlay", "_close_open_freeze", "_show_loading", "_notify_resource_quarantined", "_request_next_throttled"):
        setattr(cell, name, Mock())
    cell._current_playback_token = lambda: "current-token"
    cell._playback_token_is_current = lambda token: token == "current-token"
    cell._playback_state_identity = lambda _context: None
    cell._current_playback_state_identity = lambda: None
    cell._native_context_is_current = lambda _context: True
    with patch.dict(sys.modules, {"mpv": SimpleNamespace(MPV=FakeMpv)}):
        cell._ensure_mpv()

    def drain():
        # Joining may reveal a replacement audio worker queued by the first.
        cursor = 0
        while cursor < len(workers):
            worker = workers[cursor]
            worker.join(3)
            assert not worker.is_alive(), "native worker did not drain"
            cursor += 1

    cell.drain = drain
    return cell


def test_audio_mode_requires_explicit_opt_in():
    for configured, expected in [(None, "lazy"), ("lazy", "lazy"), ("continuous", "continuous"), (" Continuous ", "continuous"), ("prepared", "prepared"), ("auto", "lazy")]:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HYPERWALL_AUDIO_MODE", None)
            if configured is not None:
                os.environ["HYPERWALL_AUDIO_MODE"] = configured
            assert audio_mode_from_env() == expected


def test_continuous_audio_controls_never_seek_or_change_tracks():
    for platform in ("win32", "darwin", "linux"):
        cell = make_cell("continuous", platform)
        assert cell._mpv.props["aid"] == "auto"
        cell._mpv.calls.clear()
        for _ in range(4):
            cell._apply_mute(False)
            cell._vol_changed(43)
            cell._vol_changed(0)
        cell._vol_changed(57)
        cell.drain()
        assert cell._mpv.props["mute"] is False
        assert cell._mpv.props["volume"] == 57.0
        assert cell._mpv.props["aid"] == "auto"
        assert cell._audio_started is True
        assert all(call[0] in {"mute", "volume"} for call in cell._mpv.calls)


def test_lazy_audio_still_disarms_and_relocks():
    for platform in ("win32", "darwin", "linux"):
        cell = make_cell("lazy", platform)
        assert cell._mpv.props["aid"] == "no"
        cell._apply_mute(False)
        cell.drain()
        assert ("seek", 12.0, "absolute+keyframes") in cell._mpv.calls
        assert cell._last_seek_ts > 0
        assert cell._mpv.props["aid"] == "auto"
        cell._apply_mute(True)
        cell.drain()
        assert cell._mpv.props["aid"] == "no"
        assert cell._audio_started is False


def test_continuous_mode_survives_reload_and_both_prefetch_paths():
    for platform in ("win32", "darwin", "linux"):
        cell = make_cell("continuous", platform)
        cell._mpv.calls.clear()
        cell._play_impl({"Id": "next", "Name": "next"}, "fixture://next")
        cell.drain()
        if uses_render_api(platform):
            cell._finish_async_play(*cell._sig_play_finished.emit.call_args.args)
        assert ("loadfile", "fixture://next") in cell._mpv.calls
        for asynchronous in (False, True):
            cell._prefetched = ({"Id": "prefetched", "Name": "prefetched"}, "fixture://prefetched", "new-session")
            if asynchronous:
                assert cell._queue_prefetched_advance() is True
                cell.drain()
                cell._finish_prefetched_advance(*cell._sig_prefetched_advance.emit.call_args.args)
            else:
                assert cell._advance_to_prefetched_impl() is True
            assert cell._mpv.props["aid"] == "auto"
            assert cell._audio_started is True
        cell._apply_mute(False)
        cell.drain()
        assert not any(call[0] == "seek" for call in cell._mpv.calls)
        assert all(call == ("aid", "auto") for call in cell._mpv.calls if call[0] == "aid")


def test_prepared_mode_uses_continuous_only_for_marked_items():
    for platform in ("win32", "darwin", "linux"):
        cell = make_cell("prepared", platform)
        for marker in (True, None, "true", True, False):
            item = {"Id": "incoming", "Name": "incoming", "_hyperwall_prepared": marker}
            cell._play_impl(item, "fixture://incoming")
            cell.drain()
            if uses_render_api(platform):
                cell._finish_async_play(*cell._sig_play_finished.emit.call_args.args)
            assert cell._continuous_audio is (marker is True)
            assert cell._audio_started is (marker is True)
            assert cell._mpv.props["aid"] == ("auto" if marker is True else "no")
            cell._mpv.calls.clear()
            cell._play_pos = 12.0
            cell._apply_mute(False)
            cell.drain()
            if marker is True:
                assert all(call[0] == "mute" for call in cell._mpv.calls)
            else:
                assert ("seek", 12.0, "absolute+keyframes") in cell._mpv.calls
            cell._apply_mute(True)
            cell.drain()


def test_original_load_restores_lazy_track_even_if_native_cache_disagrees():
    cell = make_cell("prepared")
    # A disarm request clears _audio_started before its native worker finishes.
    cell._audio_started = False
    cell._mpv.props["aid"] = "auto"
    cell._play_impl({"Id": "original", "Name": "original"}, "fixture://original")
    assert cell._mpv.props["aid"] == "no"
    assert cell._audio_started is False


def test_async_reload_restores_volume_after_outgoing_write_is_cancelled():
    for platform in ("darwin", "linux"):
        cell = make_cell("continuous", platform)
        cell.vol_slider.value.return_value = 51
        with cell._audio_arm_call_lock:
            cell._vol_changed(51)
            # The public play() path invalidates writes against the old item
            # before _play_impl starts a new generation.
            assert cell._cancel_audio_arm(0) is True
            cell._play_impl({"Id": "next", "Name": "next"}, "fixture://next")
        cell.drain()
        assert cell._mpv.props["volume"] == 70.0
        cell._finish_async_play(*cell._sig_play_finished.emit.call_args.args)
        cell.drain()
        assert cell._mpv.props["volume"] == 51.0
        assert cell._mpv.props["mute"] is False
        assert not any(call[0] == "seek" for call in cell._mpv.calls)


def test_prepared_mode_applies_next_items_policy_before_playlist_advance():
    for asynchronous in (False, True):
        cell = make_cell("prepared")
        for marker in (True, False, True):
            item = {"Id": "next", "Name": "next", "_hyperwall_prepared": marker}
            cell._prefetched = (item, "fixture://next", "session")
            cell._mpv.calls.clear()
            if asynchronous:
                assert cell._queue_prefetched_advance() is True
                cell.drain()
                cell._finish_prefetched_advance(*cell._sig_prefetched_advance.emit.call_args.args)
            else:
                assert cell._advance_to_prefetched_impl() is True
            assert cell._mpv.calls[0] == ("aid", "auto" if marker else "no")
            assert cell._mpv.calls.index(("playlist-next",)) > 0
            assert cell._continuous_audio is marker
            assert cell._audio_started is marker
            cell.drain()


def test_unmute_during_prepared_advance_does_not_seek_incoming_video():
    cell = make_cell("prepared", "linux")
    cell._prefetched = ({"Id": "next", "Name": "next", "_hyperwall_prepared": True}, "fixture://next", "session")
    cell._mpv.calls.clear()
    assert cell._queue_prefetched_advance() is True
    cell.drain()
    # Native playlist-next has completed; Qt has not committed the new item.
    cell._apply_mute(False)
    cell.drain()
    cell._finish_prefetched_advance(*cell._sig_prefetched_advance.emit.call_args.args)
    cell.drain()
    assert cell._continuous_audio is True
    assert cell._mpv.props["mute"] is False
    assert cell._mpv.props["aid"] == "auto"
    assert not any(call[0] == "seek" for call in cell._mpv.calls)


def test_render_api_audio_controls_return_while_native_call_is_blocked():
    # Unlike the former macOS-only check in the Windows-only Qt suite, this
    # exercises both render-API branches on every host without live media.
    for platform in ("darwin", "linux"):
        for mode, property_name, action in (
            ("lazy", "aid", lambda cell: cell._apply_mute(False)),
            ("continuous", "mute", lambda cell: cell._apply_mute(False)),
            ("continuous", "volume", lambda cell: cell._vol_changed(51)),
        ):
            cell = make_cell(mode, platform)
            cell._mpv.block_property = property_name
            try:
                before = time.monotonic()
                action(cell)
                assert time.monotonic() - before < 0.1
                assert cell._mpv.entered.wait(1), "native call never started"
                assert not cell._mpv.release.is_set()
            finally:
                cell._mpv.release.set()
                cell.drain()


def test_lazy_audio_refills_do_not_quarantine_the_resource():
    for platform in ("win32", "darwin", "linux"):
        cell = make_cell("lazy", platform)
        for _ in range(3):
            cell._apply_mute(False)
            cell.drain()
            cell._handle_buffering(None, True)
            cell._handle_buffering(None, False)
            cell._apply_mute(True)
            cell.drain()
        assert cell._freeze_postseek_count == 3
        assert cell._starvation_track_events == 0
        assert cell._resource_quarantined is False
        cell._request_next_throttled.assert_not_called()
        cell._begin_track({"Id": "new", "Name": "new"})
        assert cell._last_seek_ts == 0.0


def run_all():
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as error:
            failed += 1
            print(f"  FAIL  {test.__name__}: {error!r}")
    print(f"\n{len(tests) - failed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
