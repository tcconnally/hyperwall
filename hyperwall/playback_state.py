from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PlaybackState(str, Enum):
    EMPTY = "empty"
    LOADING = "loading"
    PLAYING = "playing"
    BUFFERING = "buffering"
    RECOVERING = "recovering"
    DRAINING = "draining"
    CLOSED = "closed"


class PlaybackEvent(str, Enum):
    LOAD_REQUESTED = "load_requested"
    LOAD_STARTED = "load_started"
    LOAD_FAILED = "load_failed"
    BUFFERING_STARTED = "buffering_started"
    BUFFERING_ENDED = "buffering_ended"
    RECOVERY_REQUESTED = "recovery_requested"
    RECOVERY_SUCCEEDED = "recovery_succeeded"
    ADVANCE_REQUESTED = "advance_requested"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class PlaybackIdentity:
    player_generation: int
    track_generation: int
    item_id: str | None
    stream_identity: str | None
    session_id: str | None


@dataclass(frozen=True)
class StateChange:
    previous: PlaybackState
    current: PlaybackState
    event: PlaybackEvent
    identity: PlaybackIdentity | None
    accepted: bool


_TRANSITIONS: dict[PlaybackState, dict[PlaybackEvent, PlaybackState]] = {
    PlaybackState.EMPTY: {
        PlaybackEvent.LOAD_REQUESTED: PlaybackState.LOADING,
        PlaybackEvent.SHUTDOWN: PlaybackState.CLOSED,
    },
    PlaybackState.LOADING: {
        PlaybackEvent.LOAD_REQUESTED: PlaybackState.LOADING,
        PlaybackEvent.LOAD_STARTED: PlaybackState.PLAYING,
        PlaybackEvent.LOAD_FAILED: PlaybackState.RECOVERING,
        PlaybackEvent.RECOVERY_REQUESTED: PlaybackState.RECOVERING,
        PlaybackEvent.SHUTDOWN: PlaybackState.CLOSED,
    },
    PlaybackState.PLAYING: {
        PlaybackEvent.LOAD_REQUESTED: PlaybackState.LOADING,
        PlaybackEvent.BUFFERING_STARTED: PlaybackState.BUFFERING,
        PlaybackEvent.RECOVERY_REQUESTED: PlaybackState.RECOVERING,
        PlaybackEvent.LOAD_FAILED: PlaybackState.RECOVERING,
        PlaybackEvent.ADVANCE_REQUESTED: PlaybackState.DRAINING,
        PlaybackEvent.SHUTDOWN: PlaybackState.CLOSED,
    },
    PlaybackState.BUFFERING: {
        PlaybackEvent.LOAD_REQUESTED: PlaybackState.LOADING,
        PlaybackEvent.BUFFERING_ENDED: PlaybackState.PLAYING,
        PlaybackEvent.RECOVERY_REQUESTED: PlaybackState.RECOVERING,
        PlaybackEvent.LOAD_FAILED: PlaybackState.RECOVERING,
        PlaybackEvent.ADVANCE_REQUESTED: PlaybackState.DRAINING,
        PlaybackEvent.SHUTDOWN: PlaybackState.CLOSED,
    },
    PlaybackState.RECOVERING: {
        PlaybackEvent.LOAD_REQUESTED: PlaybackState.LOADING,
        PlaybackEvent.LOAD_FAILED: PlaybackState.RECOVERING,
        PlaybackEvent.RECOVERY_REQUESTED: PlaybackState.RECOVERING,
        PlaybackEvent.RECOVERY_SUCCEEDED: PlaybackState.PLAYING,
        PlaybackEvent.SHUTDOWN: PlaybackState.CLOSED,
    },
    PlaybackState.DRAINING: {
        PlaybackEvent.LOAD_REQUESTED: PlaybackState.LOADING,
        PlaybackEvent.SHUTDOWN: PlaybackState.CLOSED,
    },
    PlaybackState.CLOSED: {
        PlaybackEvent.SHUTDOWN: PlaybackState.CLOSED,
    },
}


class CellPlaybackController:
    def __init__(self) -> None:
        self._state = PlaybackState.EMPTY
        self._identity: PlaybackIdentity | None = None

    @property
    def state(self) -> PlaybackState:
        return self._state

    @property
    def identity(self) -> PlaybackIdentity | None:
        return self._identity

    def is_current(self, identity: PlaybackIdentity | None) -> bool:
        return identity is not None and identity == self._identity

    def transition(
        self,
        event: PlaybackEvent | str,
        identity: PlaybackIdentity | None = None,
    ) -> StateChange:
        event = PlaybackEvent(event)
        previous = self._state
        if event is PlaybackEvent.LOAD_REQUESTED:
            if identity is None:
                return StateChange(previous, previous, event, self._identity, False)
            candidate_identity = identity
        else:
            candidate_identity = self._identity
            if identity is not None and identity != self._identity:
                return StateChange(previous, previous, event, self._identity, False)
        next_state = _TRANSITIONS.get(self._state, {}).get(event)
        if next_state is None:
            return StateChange(previous, previous, event, self._identity, False)
        if event is PlaybackEvent.LOAD_REQUESTED:
            self._identity = candidate_identity
        self._state = next_state
        return StateChange(previous, next_state, event, self._identity, True)
