# Contract tests for Emby session ownership and cleanup.
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperwall.playback_plan import PlaybackPlan
from hyperwall.resource_governor import ResourceGovernor
from hyperwall.session_broker import EmbySessionBroker, SessionRecord


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class _Client:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def post(self, path, *, json, timeout):
        self.calls.append({"path": path, "json_keys": sorted(json), "timeout": timeout})
        status = self.statuses.pop(0) if self.statuses else 204
        return _Response(status)


def _plan(transcode=True):
    return PlaybackPlan(
        item_id="item",
        server_mode="server_transcode" if transcode else "direct",
        client_decoder="no",
        requires_transcode_lease=transcode,
        reason="test",
        source_fps=30.0,
        source_bitrate_mbps=80.0 if transcode else 20.0,
    )


def _broker(statuses=(204,), outage=None):
    client = _Client(statuses)
    governor = ResourceGovernor(max_server_transcodes=1)
    state = {"outage": bool(outage)}
    submitted = []

    def submit(fn, label):
        submitted.append(label)
        fn()
        return object()

    broker = EmbySessionBroker(
        client=client,
        submit=submit,
        in_outage=lambda: state["outage"],
        governor=governor,
        registry_limit=4,
        cleanup_limit=8,
    )
    return broker, client, governor, state, submitted


def test_register_owns_session_record_and_admission():
    broker, _client, governor, _state, _submitted = _broker()
    plan = _plan()
    assert broker.admit(plan, "session-a") is True
    result = broker.register(SessionRecord("item", "session-a", plan))

    assert result.accepted is True
    assert result.evicted == ()
    assert broker.records() == (SessionRecord("item", "session-a", plan),)
    assert broker.admission_available() is True
    assert governor.active_server_transcodes == 1


def test_successful_stop_removes_record_and_releases_lease():
    broker, client, governor, _state, submitted = _broker((204,))
    plan = _plan()
    broker.admit(plan, "session-a")
    broker.register(SessionRecord("item", "session-a", plan))

    broker.stop("item", "session-a")

    assert submitted == ["stop-session"]
    assert client.calls[0]["path"] == "/Sessions/Playing/Stopped"
    assert broker.records() == ()
    assert broker.pending_records() == ()
    assert governor.active_server_transcodes == 0


def test_failed_stop_remains_registered_for_retry():
    broker, _client, governor, _state, _submitted = _broker((500, 500))
    plan = _plan()
    broker.admit(plan, "session-a")
    record = SessionRecord("item", "session-a", plan)
    broker.register(record)

    broker.stop("item", "session-a")

    assert broker.records() == (record,)
    assert broker.pending_records() == (record,)
    assert governor.active_server_transcodes == 1


def test_outage_defers_stop_without_submitting_work():
    broker, _client, governor, state, submitted = _broker((204,), outage=True)
    plan = _plan()
    broker.admit(plan, "session-a")
    record = SessionRecord("item", "session-a", plan)
    broker.register(record)

    broker.stop("item", "session-a")
    assert submitted == []
    assert broker.pending_records() == (record,)
    assert governor.active_server_transcodes == 1

    state["outage"] = False
    broker.retry_pending()
    assert submitted == ["stop-session"]
    assert broker.records() == ()
    assert governor.active_server_transcodes == 0


def test_stop_requests_are_idempotent_while_in_flight():
    broker, _client, _governor, _state, submitted = _broker((204,))
    plan = _plan(transcode=False)
    record = SessionRecord("item", "session-a", plan)
    broker.register(record)
    broker.stop("item", "session-a")
    broker.stop("item", "session-a")

    assert submitted == ["stop-session"]



def test_registry_eviction_returns_record_for_cleanup():
    client = _Client((204,))
    governor = ResourceGovernor(max_server_transcodes=0)
    broker = EmbySessionBroker(
        client=client,
        submit=lambda fn, label: object(),
        in_outage=lambda: False,
        governor=governor,
        registry_limit=1,
        cleanup_limit=4,
    )
    first = SessionRecord("item-a", "session-a", _plan(transcode=False))
    second = SessionRecord("item-b", "session-b", _plan(transcode=False))
    assert broker.register(first).accepted
    result = broker.register(second)

    assert result.accepted
    assert result.evicted == (first,)
    assert broker.records() == (second,)
    assert broker.pending_records() == (first,)


def test_executor_submission_failure_is_retained_for_retry():
    broker, _client, governor, _state, _submitted = _broker((204,))
    plan = _plan()
    broker.admit(plan, "session-a")
    record = SessionRecord("item", "session-a", plan)
    broker.register(record)

    def submit_raises(_fn, _label):
        raise RuntimeError("executor closed")

    broker._submit = submit_raises
    broker.stop("item", "session-a")

    assert broker.pending_records() == (record,)
    assert broker.records() == (record,)
    assert governor.active_server_transcodes == 1


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
