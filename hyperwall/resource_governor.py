from __future__ import annotations

from dataclasses import dataclass
import threading

from .playback_plan import PlaybackPlan


@dataclass(frozen=True)
class ResourceSnapshot:
    active_server_transcodes: int
    max_server_transcodes: int
    lease_keys: tuple[str, ...]


class ResourceGovernor:
    def __init__(self, max_server_transcodes: int) -> None:
        self._max_server_transcodes = max(0, int(max_server_transcodes))
        self._leases: set[str] = set()
        self._lock = threading.Lock()

    @property
    def active_server_transcodes(self) -> int:
        with self._lock:
            return len(self._leases)

    @property
    def max_server_transcodes(self) -> int:
        return self._max_server_transcodes

    def can_admit(self, plan: PlaybackPlan, lease_key: str | None = None) -> bool:
        if not plan.requires_transcode_lease:
            return True
        with self._lock:
            return (
                self._max_server_transcodes <= 0
                or lease_key in self._leases
                or len(self._leases) < self._max_server_transcodes
            )

    def acquire(self, plan: PlaybackPlan, lease_key: str) -> bool:
        if not lease_key:
            raise ValueError("a resource lease requires a non-empty key")
        if not plan.requires_transcode_lease:
            return True
        with self._lock:
            if lease_key in self._leases:
                return True
            if (
                self._max_server_transcodes > 0
                and len(self._leases) >= self._max_server_transcodes
            ):
                return False
            self._leases.add(lease_key)
            return True

    def release(self, lease_key: str) -> bool:
        with self._lock:
            if lease_key not in self._leases:
                return False
            self._leases.remove(lease_key)
            return True

    def snapshot(self) -> ResourceSnapshot:
        with self._lock:
            return ResourceSnapshot(
                active_server_transcodes=len(self._leases),
                max_server_transcodes=self._max_server_transcodes,
                lease_keys=tuple(sorted(self._leases)),
            )
