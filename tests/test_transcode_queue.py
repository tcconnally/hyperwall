"""Targeted conversion tests with fake HTTP and real bounded daemon workers.

No media server, credentials, GUI, or conversion processes are used.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hyperwall.transcode_queue import FailureTranscodeQueue, ensure_targeted_job


class HTTPError(RuntimeError):
    pass


class Response:
    def __init__(self, body=None, status=200):
        self.body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            raise HTTPError("synthetic authenticated URL must not be persisted")

    def json(self):
        return self.body


class FakeClient:
    def __init__(self):
        self.backend = SimpleNamespace(name="emby")
        self.server_url = "http://fixture.invalid"
        self.user_id = "synthetic-user"
        self.targets = [{"Id": "separate-output", "Name": "Prepared"}]
        self.item = {"Id": "video-1", "Type": "Video", "IsFolder": False}
        self.job_items = []
        self.total_count = None
        self.calls = []
        self.status = 200
        self.accept_visible = True
        self.post_error = None
        self.before_post = None
        self.block_get = False
        self.get_entered, self.get_release = threading.Event(), threading.Event()
        self.posted = threading.Event()

    def get(self, path, **kwargs):
        self.calls.append(("GET", path, kwargs, threading.get_ident()))
        assert kwargs["timeout"] == 10
        if self.block_get:
            self.get_entered.set()
            assert self.get_release.wait(3), "Blocked fake HTTP was not released"
        if path == "/Sync/Targets":
            body = list(self.targets)
        elif path.startswith("/Users/"):
            body = dict(self.item)
        elif path == "/Sync/JobItems":
            assert kwargs["params"] == {"TargetId": "separate-output"}
            body = {"Items": list(self.job_items), "TotalRecordCount":
                    len(self.job_items) if self.total_count is None else self.total_count}
        else:
            raise AssertionError(f"Unexpected endpoint: {path}")
        return Response(body, self.status)

    def post(self, path, **kwargs):
        self.calls.append(("POST", path, kwargs, threading.get_ident()))
        assert kwargs["timeout"] == 10
        if self.before_post:
            self.before_post()
        if path == "/Sync/Jobs":
            if self.accept_visible:
                self.job_items.append({"ItemId": "video-1", "Id": "job-item", "JobId": "job", "Status": "Queued"})
        elif path == "/Sync/JobItems/cancelled-item/Enable":
            if self.accept_visible:
                self.job_items[0]["Status"] = "Queued"
        else:
            raise AssertionError(f"Unexpected write endpoint: {path}")
        self.posted.set()
        if self.post_error:
            raise self.post_error
        return Response({"Id": "job", "JobItemIds": ["job-item"]}, self.status)

    @property
    def writes(self):
        return [call for call in self.calls if call[0] == "POST"]


class ImmediateStop:
    """Avoid real backoff delays when exercising the synchronous worker body."""
    def is_set(self):
        return False

    def wait(self, _seconds):
        return False


def process_fixture(directory, client=None):
    worker = object.__new__(FailureTranscodeQueue)
    worker.client = client or FakeClient()
    worker.target_name = "Prepared"
    worker.directory = Path(directory)
    worker._stop = ImmediateStop()
    worker.seen = set()
    worker.pending = queue.Queue(maxsize=128)
    return worker


def evidence():
    return {"item_id": "video-1", "reason": "malformed_stream", "state": "pending", "observed_at": 1.0}


def read_record(worker):
    return json.loads((worker.directory / "video-1.json").read_text())


def expect_error(function, expected):
    try:
        function()
    except ValueError as exc:
        assert str(exc) == expected, repr(exc)
    else:
        raise AssertionError(f"Expected {expected}")


@contextmanager
def live_worker(client=None):
    client = client or FakeClient()
    with tempfile.TemporaryDirectory() as directory:
        worker = FailureTranscodeQueue(client, Path(directory), "Prepared")
        try:
            yield worker, client
        finally:
            worker.stop()
            client.get_release.set()
            worker.thread.join(timeout=3)
            assert not worker.thread.is_alive(), "Daemon test worker did not stop"


def test_creates_only_one_confirmed_individual_video_job():
    client = FakeClient()
    result = ensure_targeted_job(client, "video-1", "Prepared")
    assert result == {"state": "queued", "job_id": "job", "job_item_id": "job-item"}
    assert len(client.writes) == 1
    body = client.writes[0][2]["json"]
    assert body["ItemIds"] == ["video-1"]
    assert body["TargetId"] == "separate-output"
    assert body["SyncNewContent"] is False and body["UnwatchedOnly"] is False
    assert (body["Container"], body["VideoCodec"], body["AudioCodec"], body["Quality"]) == ("mp4", "h264", "aac", "4000000")


def test_remote_existing_jobs_are_reused_without_duplicate_writes():
    for status in ("Queued", "Converting", "ReadyToTransfer", "Transferring", "Synced"):
        client = FakeClient()
        client.job_items = [{"ItemId": "video-1", "Id": "existing", "JobId": "job", "Status": status}]
        result = ensure_targeted_job(client, "video-1", "Prepared")
        assert result["state"] == ("existing_copy" if status == "Synced" else "queued")
        assert result["job_item_id"] == "existing"
        assert not client.writes


def test_cancelled_job_is_enabled_instead_of_duplicated():
    client = FakeClient()
    client.job_items = [{"ItemId": "video-1", "Id": "cancelled-item", "JobId": "job", "Status": "Cancelled"}]
    result = ensure_targeted_job(client, "video-1", "Prepared")
    assert result["state"] == "queued"
    assert [write[1] for write in client.writes] == ["/Sync/JobItems/cancelled-item/Enable"]


def test_original_missing_or_ambiguous_output_target_is_rejected():
    for targets in ([], [{"Id": "originalmediafolder", "Name": "Prepared"}],
                    [{"Id": "originalmediafolderreplace", "Name": "Prepared"}],
                    [{"Id": None, "Name": "Prepared"}],
                    [{"Id": "one", "Name": "Prepared"}, {"Id": "two", "Name": "Prepared"}]):
        client = FakeClient()
        client.targets = targets
        expect_error(lambda: ensure_targeted_job(client, "video-1", "Prepared"), "separate_output_target_required")
        assert not client.writes


def test_folders_wrong_identity_nonvideo_and_nonemby_are_rejected():
    for item in ({"Id": "video-1", "Type": "Video", "IsFolder": True},
                 {"Id": "other", "Type": "Video"},
                 {"Id": "video-1", "Type": "Folder"},
                 {"Id": "video-1", "Type": "Audio"}):
        client = FakeClient()
        client.item = item
        expect_error(lambda: ensure_targeted_job(client, "video-1", "Prepared"), "individual_video_required")
        assert not client.writes
    client = FakeClient()
    client.backend.name = "jellyfin"
    expect_error(lambda: ensure_targeted_job(client, "video-1", "Prepared"), "emby_required")
    assert not client.calls


def test_incomplete_inventory_and_failed_prior_conversion_fail_closed():
    client = FakeClient()
    client.total_count = 100
    expect_error(lambda: ensure_targeted_job(client, "video-1", "Prepared"), "incomplete_sync_inventory")
    assert not client.writes
    for status in ("Failed", "Removing", "Removed", "Unknown"):
        client = FakeClient()
        client.job_items = [{"ItemId": "video-1", "Id": "failed", "Status": status}]
        expect_error(lambda: ensure_targeted_job(client, "video-1", "Prepared"), "existing_conversion_needs_review")
        assert not client.writes


def test_evidence_is_persisted_before_server_write_and_success_is_verified():
    with tempfile.TemporaryDirectory() as directory:
        worker = process_fixture(directory)
        observed = []
        worker.client.before_post = lambda: observed.append(read_record(worker))
        worker._process(evidence())
        assert observed and observed[0]["reason"] == "malformed_stream"
        assert observed[0]["item_id"] == "video-1"
        assert read_record(worker)["state"] == "queued"
        assert read_record(worker)["job_item_id"] == "job-item"
        if os.name != "nt":
            assert (worker.directory / "video-1.json").stat().st_mode & 0o777 == 0o600
        raw = (worker.directory / "video-1.json").read_text()
        assert "fixture.invalid" not in raw and "Name" not in raw


def test_file_and_directory_evidence_are_fsynced_before_server_mutation():
    with tempfile.TemporaryDirectory() as directory:
        worker = process_fixture(directory)
        events = []
        real_fsync = os.fsync

        def fsync(fd):
            events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            real_fsync(fd)

        def before_post():
            assert events[:4] == ["file", "directory", "file", "directory"]
            assert read_record(worker)["submission_started"] is True
            events.append("post")

        worker.client.before_post = before_post
        with patch("hyperwall.transcode_queue.os.fsync", side_effect=fsync):
            worker._process(evidence())
        assert "post" in events


def test_status_failure_retains_evidence_without_sensitive_exception_text():
    with tempfile.TemporaryDirectory() as directory:
        worker = process_fixture(directory)
        worker.client.status = 503
        worker._process(evidence())
        record = read_record(worker)
        assert record["state"] == "pending" and record["reason"] == "malformed_stream"
        assert record["error"] == "HTTPError"
        assert "authenticated" not in json.dumps(record)
        assert not worker.client.writes


def test_failed_evidence_write_prevents_server_mutation():
    with tempfile.TemporaryDirectory() as directory:
        worker = process_fixture(directory)
        worker._save = Mock(side_effect=OSError("disk unavailable"))
        try:
            worker._process(evidence())
        except OSError:
            pass
        else:
            raise AssertionError("Missing evidence write did not stop processing")
        assert not worker.client.calls


def test_delayed_post_visibility_does_not_duplicate_server_mutation():
    with tempfile.TemporaryDirectory() as directory:
        worker = process_fixture(directory)
        worker.client.accept_visible = False
        worker._process(evidence())
        assert len(worker.client.writes) == 1, "Accepted job was posted again while visibility lagged"
        assert read_record(worker)["state"] not in {"queued", "existing_copy"}


def test_lost_post_response_does_not_duplicate_server_mutation():
    with tempfile.TemporaryDirectory() as directory:
        worker = process_fixture(directory)
        worker.client.accept_visible = False
        worker.client.post_error = TimeoutError("synthetic secret authenticated URL")
        worker._process(evidence())
        assert len(worker.client.writes) == 1, "Uncertain accepted request was blindly posted again"
        assert "secret" not in json.dumps(read_record(worker))


def test_uncertain_submission_remains_read_only_after_restart_until_confirmed():
    with tempfile.TemporaryDirectory() as directory:
        first = process_fixture(directory)
        first.client.accept_visible = False
        first.client.post_error = TimeoutError("response lost")
        first._process(evidence())
        persisted = read_record(first)
        assert persisted["submission_started"] is True
        restarted = process_fixture(directory, first.client)
        restarted._process(persisted)
        assert len(first.client.writes) == 1
        assert read_record(restarted)["state"] == "pending"
        first.client.job_items = [{"ItemId": "video-1", "Id": "eventually-visible", "JobId": "job", "Status": "Converting"}]
        restarted._process(read_record(restarted))
        assert len(first.client.writes) == 1
        assert read_record(restarted)["state"] == "queued"
        assert read_record(restarted)["job_item_id"] == "eventually-visible"


def test_submit_rejects_invalid_reason_folder_identity_and_stopped_queue():
    with live_worker() as (worker, client):
        for item, reason in (({"Id": "video-1"}, "high_bitrate"),
                             ({"Id": "video-1"}, "vo_drops"),
                             ({"Id": "video-1", "IsFolder": True}, "malformed_stream"),
                             ({"Id": "../../escape"}, "malformed_stream"),
                             ({}, "malformed_stream")):
            assert worker.submit(item, reason) is False
        worker.stop()
        assert worker.submit({"Id": "video-1"}, "malformed_stream") is False
        assert not client.calls


def test_submit_and_stop_stay_nonblocking_while_http_is_blocked():
    client = FakeClient()
    client.block_get = True
    with live_worker(client) as (worker, client):
        caller = threading.get_ident()
        start = time.monotonic()
        assert worker.submit({"Id": "video-1"}, "malformed_stream")
        assert time.monotonic() - start < .1
        assert client.get_entered.wait(1)
        assert not worker.submit({"Id": "video-1"}, "decoder_recovery_exhausted")
        start = time.monotonic()
        assert worker.submit({"Id": "video-2"}, "decoder_recovery_exhausted")
        worker.stop()
        assert time.monotonic() - start < .1, "GUI submit/stop waited for blocked server I/O"
        assert worker.thread.daemon
        assert all(call[3] != caller for call in client.calls)


def test_pending_evidence_resumes_and_is_deduplicated_during_resume():
    client = FakeClient()
    client.block_get = True
    with tempfile.TemporaryDirectory() as directory:
        initial = FailureTranscodeQueue(client, Path(directory), "Prepared")
        initial.stop()
        initial.thread.join(timeout=1)
        initial._save(evidence())
        resumed = FailureTranscodeQueue(client, Path(directory), "Prepared")
        try:
            assert client.get_entered.wait(1)
            assert not resumed.submit({"Id": "video-1"}, "malformed_stream"), "Persisted in-flight evidence was enqueued twice"
            assert read_record(resumed)["reason"] == "malformed_stream"
        finally:
            resumed.stop()
            client.get_release.set()
            resumed.thread.join(timeout=3)
            assert not resumed.thread.is_alive()


def test_bad_ledger_records_do_not_stop_new_valid_evidence():
    with tempfile.TemporaryDirectory() as directory:
        client = FakeClient()
        initial = FailureTranscodeQueue(client, Path(directory), "Prepared")
        initial.stop()
        initial.thread.join(timeout=1)
        initial.directory.mkdir(parents=True, exist_ok=True)
        for index, value in enumerate(([], {"state": "pending", "reason": [], "item_id": "video-1"},
                                       {"state": "pending", "reason": "high_bitrate", "item_id": "video-1"},
                                       {"state": "pending", "reason": "malformed_stream", "item_id": "../unsafe"})):
            (initial.directory / f"bad-{index}.json").write_text(json.dumps(value))
        resumed = FailureTranscodeQueue(client, Path(directory), "Prepared")
        try:
            assert resumed.submit({"Id": "video-1"}, "malformed_stream")
            assert client.posted.wait(1), "Invalid ledger stopped the worker before valid evidence"
            assert len(client.writes) == 1
        finally:
            resumed.stop()
            resumed.thread.join(timeout=3)
            assert not resumed.thread.is_alive()


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
