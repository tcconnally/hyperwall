# Contract tests for the cell playback state machine.
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperwall.playback_state import (
    CellPlaybackController,
    PlaybackEvent,
    PlaybackIdentity,
    PlaybackState,
)


def _identity(track=1, item="item", stream="stream", session="session"):
    return PlaybackIdentity(2, track, item, stream, session)


def test_normal_load_and_buffer_lifecycle():
    controller = CellPlaybackController()
    identity = _identity()

    assert controller.transition(PlaybackEvent.LOAD_REQUESTED, identity).accepted
    assert controller.state is PlaybackState.LOADING
    assert controller.transition(PlaybackEvent.LOAD_STARTED, identity).accepted
    assert controller.state is PlaybackState.PLAYING
    assert controller.transition(PlaybackEvent.BUFFERING_STARTED, identity).accepted
    assert controller.state is PlaybackState.BUFFERING
    assert controller.transition(PlaybackEvent.BUFFERING_ENDED, identity).accepted
    assert controller.state is PlaybackState.PLAYING


def test_recovery_is_explicit_and_returns_to_playing():
    controller = CellPlaybackController()
    identity = _identity()
    controller.transition(PlaybackEvent.LOAD_REQUESTED, identity)
    controller.transition(PlaybackEvent.LOAD_STARTED, identity)

    changed = controller.transition(PlaybackEvent.RECOVERY_REQUESTED, identity)
    assert changed.accepted
    assert controller.state is PlaybackState.RECOVERING
    assert controller.transition(PlaybackEvent.RECOVERY_SUCCEEDED, identity).accepted
    assert controller.state is PlaybackState.PLAYING


def test_stale_native_event_is_ignored_without_mutating_state():
    controller = CellPlaybackController()
    current = _identity(track=2)
    stale = _identity(track=1)
    controller.transition(PlaybackEvent.LOAD_REQUESTED, current)
    controller.transition(PlaybackEvent.LOAD_STARTED, current)

    changed = controller.transition(PlaybackEvent.BUFFERING_STARTED, stale)
    assert changed.accepted is False
    assert changed.previous is PlaybackState.PLAYING
    assert changed.current is PlaybackState.PLAYING
    assert controller.identity == current


def test_new_load_replaces_identity_and_enters_loading():
    controller = CellPlaybackController()
    first = _identity(track=1)
    second = _identity(track=2, item="next", stream="next-stream")
    controller.transition(PlaybackEvent.LOAD_REQUESTED, first)
    controller.transition(PlaybackEvent.LOAD_STARTED, first)

    changed = controller.transition(PlaybackEvent.LOAD_REQUESTED, second)
    assert changed.accepted
    assert controller.state is PlaybackState.LOADING
    assert controller.identity == second


def test_invalid_transition_is_rejected():
    controller = CellPlaybackController()
    identity = _identity()

    changed = controller.transition(PlaybackEvent.BUFFERING_ENDED, identity)
    assert changed.accepted is False
    assert controller.state is PlaybackState.EMPTY


def test_shutdown_is_terminal_and_rejects_late_events():
    controller = CellPlaybackController()
    identity = _identity()
    controller.transition(PlaybackEvent.LOAD_REQUESTED, identity)
    controller.transition(PlaybackEvent.SHUTDOWN, identity)

    assert controller.state is PlaybackState.CLOSED
    late = controller.transition(PlaybackEvent.LOAD_STARTED, identity)
    assert late.accepted is False
    assert controller.state is PlaybackState.CLOSED


def test_advance_enters_draining_before_next_load():
    controller = CellPlaybackController()
    identity = _identity()
    controller.transition(PlaybackEvent.LOAD_REQUESTED, identity)
    controller.transition(PlaybackEvent.LOAD_STARTED, identity)

    assert controller.transition(PlaybackEvent.ADVANCE_REQUESTED, identity).accepted
    assert controller.state is PlaybackState.DRAINING



def test_video_cell_has_compatibility_state_hook():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = open(os.path.join(root, "hyperwall", "cell.py"), encoding="utf-8").read()
    assert "CellPlaybackController" in source
    assert "self._playback_controller = CellPlaybackController()" in source
    assert "self._playback_controller.transition" in source
    assert "PlaybackEvent.LOAD_REQUESTED" in source
    assert "PlaybackEvent.SHUTDOWN" in source


def run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS  {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\\n{passed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    raise SystemExit(run_all())
