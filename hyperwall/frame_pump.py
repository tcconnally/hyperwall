"""Thread-safe coalescing for libmpv-to-Qt frame notifications."""
from __future__ import annotations

import threading
from typing import Any


class FramePumpGate:
    """Bound queued GUI work while retaining the newest-frame signal.

    libmpv invokes the update callback on a native worker thread while Qt
    painting runs on the GUI thread.  ``request`` marks that a new frame is
    available and admits at most one queued GUI notification.  A frame that
    arrives while a paint is in progress causes ``finish_paint`` to request one
    follow-up notification, rather than being lost or creating an event burst.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = False
        self._dirty = False
        self._closed = False
        self._callbacks = 0
        self._queued_updates = 0
        self._coalesced_callbacks = 0
        self._ignored_callbacks = 0

    def request(self) -> bool:
        """Record a frame callback and return whether to queue an update."""
        with self._lock:
            self._callbacks += 1
            if self._closed:
                self._ignored_callbacks += 1
                return False
            self._dirty = True
            if self._pending:
                self._coalesced_callbacks += 1
                return False
            self._pending = True
            self._queued_updates += 1
            return True

    def begin_paint(self) -> None:
        """Mark the start of a GUI paint and consume the current dirty bit."""
        with self._lock:
            if not self._closed:
                self._dirty = False

    def finish_paint(self) -> bool:
        """Finish a paint and report whether a follow-up update is needed."""
        with self._lock:
            if self._closed:
                self._pending = False
                self._dirty = False
                return False
            if self._dirty:
                # Keep the pending state asserted while the caller emits the
                # follow-up queued signal. A racing callback is coalesced.
                self._dirty = False
                self._queued_updates += 1
                return True
            self._pending = False
            return False

    def close(self) -> None:
        """Reject future callbacks and clear any queued state."""
        with self._lock:
            self._closed = True
            self._pending = False
            self._dirty = False

    def snapshot(self) -> dict[str, Any]:
        """Return bounded counters and current gate state."""
        with self._lock:
            return {
                "callbacks": self._callbacks,
                "queued_updates": self._queued_updates,
                "coalesced_callbacks": self._coalesced_callbacks,
                "ignored_callbacks": self._ignored_callbacks,
                "pending": self._pending,
                "closed": self._closed,
            }


__all__ = ["FramePumpGate"]
