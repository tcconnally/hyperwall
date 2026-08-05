"""
Hyperwall — pure multi-source playlist management (no PyQt / mpv / Emby).

A PlaylistManager owns one shuffled queue per *source group*. Cells bound to
the same group share a queue, giving global de-dup within that group: no two
of those cells replay the same item until the group's whole pool is exhausted.

The default wall uses a single group ("all"), so behavior is identical to the
prior single-deque implementation. Per-monitor / per-cell sourcing (Epic 4)
simply assigns different group keys to different cells.

Pure and deterministic under an injected shuffle, so it's unit-testable without
a display server or a live Emby instance.
"""

from __future__ import annotations

import random
from collections import deque
from typing import Any, Callable

Item = dict[str, Any]

DEFAULT_GROUP = "all"


class PlaylistManager:
    """Per-source-group shuffled playout queues with global de-dup per group."""

    def __init__(self, shuffle: Callable[[list], None] = random.shuffle) -> None:
        # group_key -> full item pool for that group
        self._pools: dict[str, list[Item]] = {}
        # group_key -> live shuffled deque (refilled from the pool when empty)
        self._queues: dict[str, deque[Item]] = {}
        self._shuffle = shuffle

    # ── pool management ───────────────────────────────────────────────────

    def set_source(self, items: list[Item], group: str = DEFAULT_GROUP) -> None:
        """Set (or replace) the item pool for a group and reset its queue."""
        self._pools[group] = list(items)
        self._queues.pop(group, None)

    def groups(self) -> list[str]:
        return list(self._pools.keys())

    def pool_size(self, group: str = DEFAULT_GROUP) -> int:
        return len(self._pools.get(group, []))

    def clear(self, group: str | None = None) -> None:
        """Drop live queues (e.g. on a filter change). Pools are preserved.

        group=None clears every group's queue; a key clears just that one.
        """
        if group is None:
            self._queues.clear()
        else:
            self._queues.pop(group, None)

    # ── playout ───────────────────────────────────────────────────────────

    def _refill(self, group: str) -> None:
        pool = self._pools.get(group, [])
        shuffled = list(pool)
        self._shuffle(shuffled)
        self._queues[group] = deque(shuffled)

    def next(self, group: str = DEFAULT_GROUP) -> Item | None:
        """Return the next item for a group, or None if the pool is empty.

        Pops from the group's live queue, refilling+reshuffling from the pool
        when the queue is exhausted (starts a fresh de-dup cycle).
        """
        if not self._pools.get(group):
            return None
        q = self._queues.get(group)
        if not q:
            self._refill(group)
            q = self._queues[group]
        return q.popleft()

    def push_front(self, group: str, item: Item) -> None:
        """Return a reserved item to the front of a group's live queue."""
        if item not in self._pools.get(group, []):
            return
        q = self._queues.setdefault(group, deque())
        q.appendleft(item)
