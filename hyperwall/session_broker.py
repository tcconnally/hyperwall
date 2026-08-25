from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any, Callable

from .playback_plan import PlaybackPlan
from .resource_governor import ResourceGovernor

logger = logging.getLogger("HyperWall")


@dataclass(frozen=True)
class SessionRecord:
    item_id: str
    session_id: str
    plan: PlaybackPlan | None = None


@dataclass(frozen=True)
class RegisterResult:
    accepted: bool
    evicted: tuple[SessionRecord, ...] = ()


class EmbySessionBroker:
    def __init__(
        self,
        *,
        client: Any,
        submit: Callable[[Callable[[], None], str], Any],
        in_outage: Callable[[], bool],
        governor: ResourceGovernor,
        registry_limit: int = 4096,
        cleanup_limit: int = 8192,
        retry_attempts: int = 2,
        retry_delay_s: float = 0.1,
    ) -> None:
        self._client = client
        self._submit = submit
        self._in_outage = in_outage
        self._governor = governor
        self._registry_limit = max(1, int(registry_limit))
        self._cleanup_limit = max(self._registry_limit, int(cleanup_limit))
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_delay_s = max(0.0, float(retry_delay_s))
        self._registry: OrderedDict[str, SessionRecord] = OrderedDict()
        self._cleanup: OrderedDict[str, SessionRecord] = OrderedDict()
        self._stop_inflight: set[str] = set()
        self._stopped: deque[str] = deque(maxlen=4096)
        self._lock = threading.Lock()

    @property
    def active_server_transcodes(self) -> int:
        return self._governor.active_server_transcodes

    def admit(self, plan: PlaybackPlan, session_id: str) -> bool:
        return self._governor.acquire(plan, session_id)

    def admission_available(self) -> bool:
        with self._lock:
            return len(set(self._registry) | set(self._cleanup)) < self._cleanup_limit

    def register(self, record: SessionRecord) -> RegisterResult:
        if not record.item_id or not record.session_id:
            return RegisterResult(False)
        if record.plan is not None and not self.admit(record.plan, record.session_id):
            return RegisterResult(False)
        evicted: list[SessionRecord] = []
        with self._lock:
            known = record.session_id in self._registry or record.session_id in self._cleanup
            if not known and len(set(self._registry) | set(self._cleanup)) >= self._cleanup_limit:
                return RegisterResult(False)
            self._cleanup.pop(record.session_id, None)
            try:
                self._stopped.remove(record.session_id)
            except ValueError:
                pass
            self._registry[record.session_id] = record
            while len(self._registry) > self._registry_limit:
                session_id, old = self._registry.popitem(last=False)
                self._cleanup[session_id] = old
                evicted.append(old)
        return RegisterResult(True, tuple(evicted))

    def records(self) -> tuple[SessionRecord, ...]:
        with self._lock:
            return tuple(self._registry.values())

    def pending_records(self) -> tuple[SessionRecord, ...]:
        with self._lock:
            return tuple(self._cleanup.values())

    def shutdown_records(self) -> tuple[SessionRecord, ...]:
        with self._lock:
            merged: OrderedDict[str, SessionRecord] = OrderedDict(self._cleanup)
            merged.update(self._registry)
            return tuple(merged.values())

    def _remember_pending_locked(self, record: SessionRecord) -> None:
        if record.session_id in self._cleanup:
            self._cleanup[record.session_id] = record
            return
        if len(set(self._registry) | set(self._cleanup)) >= self._cleanup_limit:
            logger.critical("Session cleanup capacity exhausted; admission remains closed.")
            return
        self._cleanup[record.session_id] = record

    def stop(
        self,
        item_id: str | None,
        session_id: str | None,
        *,
        plan: PlaybackPlan | None = None,
    ) -> None:
        if not item_id or not session_id:
            return
        with self._lock:
            if session_id in self._stopped or session_id in self._stop_inflight:
                return
            record = self._registry.get(session_id) or self._cleanup.get(session_id)
            if record is None:
                record = SessionRecord(item_id, session_id, plan)
            if self._in_outage():
                self._remember_pending_locked(record)
                return
            self._stop_inflight.add(session_id)

        def worker() -> None:
            success = False
            last_error: Exception | None = None
            for attempt in range(self._retry_attempts):
                try:
                    response = self._client.post(
                        "/Sessions/Playing/Stopped",
                        json={
                            "ItemId": record.item_id,
                            "PlaySessionId": record.session_id,
                            "PositionTicks": 0,
                        },
                        timeout=5,
                    )
                    if 200 <= response.status_code < 300:
                        success = True
                        break
                    last_error = RuntimeError(
                        f"HTTP {response.status_code} from stop-session"
                    )
                except Exception as exc:
                    last_error = exc
                if attempt + 1 < self._retry_attempts and self._retry_delay_s:
                    time.sleep(self._retry_delay_s)
            with self._lock:
                self._stop_inflight.discard(record.session_id)
                if success:
                    self._registry.pop(record.session_id, None)
                    self._cleanup.pop(record.session_id, None)
                    self._stopped.append(record.session_id)
                else:
                    self._remember_pending_locked(record)
            if success:
                self._governor.release(record.session_id)
            elif last_error is not None:
                logger.error("Session stop remains pending after bounded retries: %s", last_error)

        try:
            future = self._submit(worker, "stop-session")
        except Exception as exc:
            logger.debug("Session stop submission failed: %s", exc)
            future = None
        if future is None:
            with self._lock:
                self._stop_inflight.discard(session_id)
                self._remember_pending_locked(record)

    def retry_pending(self) -> None:
        if self._in_outage():
            return
        for record in self.pending_records():
            self.stop(record.item_id, record.session_id, plan=record.plan)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "active_sessions": len(self._registry),
                "pending_stops": len(self._cleanup),
                "inflight_stops": len(self._stop_inflight),
                "active_server_transcodes": self._governor.active_server_transcodes,
            }
