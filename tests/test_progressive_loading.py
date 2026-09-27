"""Progressive library admission without Qt, media, credentials or network I/O.

Run the production loading/update methods against a deterministic fake Emby
response stream. A blocked response proves that first playback admission does
not wait for the full library, while receipt policy and current cells survive.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
import logging
import os
import random
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hyperwall import constants
from hyperwall.playback_plan import select_playback_candidates
from hyperwall.playlist import DEFAULT_GROUP, PlaylistManager
from hyperwall.renditions import prefer_rendition
from hyperwall.reliability import allow_transcode_prefetch
from hyperwall.soak_filter import apply_initial_filter
from hyperwall.transcode_queue import FAILURE_REASONS


LOGGER = logging.getLogger("progressive-loading-test")


def production_class(filename, name, methods, namespace):
    path = ROOT / "hyperwall" / filename
    tree = ast.parse(path.read_text(), filename=str(path))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    cls.bases, cls.decorator_list = [], []
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    assert {node.name for node in cls.body} == set(methods)
    for method in cls.body:
        method.decorator_list = []
    tree.body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), cls]
    exec(compile(ast.fix_missing_locations(tree), str(path), "exec"), namespace)
    return namespace[name]


def original(index):
    return {"Id": str(index), "Name": f"fixture-{index}",
            "UserData": {"IsFavorite": index % 2 == 0}, "MediaSources": [],
            "MediaStreams": [{"Type": "Video", "Codec": "h264", "Width": 1920,
                              "Height": 1080, "AverageFrameRate": 30, "BitRate": 4_000_000}]}


def source(item_id):
    return {"Id": "prepared-" + item_id, "Protocol": "File", "Container": "mp4",
            "SupportsDirectStream": True,
            "Path": f"/hyperwall/mv/{item_id}.mp4", "Size": 10_000, "RunTimeTicks": 120_000_000,
            "MediaStreams": [{"Type": "Video", "Codec": "h264", "Width": 1920,
                              "Height": 1080, "AverageFrameRate": 30, "BitRate": 4_000_000},
                             {"Type": "Audio", "Codec": "aac", "Channels": 2}]}


class Response:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class Signal:
    def __init__(self):
        self.emitted = []
        self.event = threading.Event()

    def emit(self, value):
        self.emitted.append(list(value) if isinstance(value, list) else value)
        self.event.set()


def client_fixture(count=12, verified=None, block_item=None):
    namespace = {"os": os, "logger": LOGGER, "prefer_rendition": prefer_rendition,
                 "requests": SimpleNamespace(RequestException=RuntimeError)}
    client_cls = production_class("emby.py", "EmbyClient", {"fetch_items", "_prefer_renditions"}, namespace)
    client = client_cls()
    client.user_id = "synthetic-user"
    client.originals = [original(index) for index in range(count)]
    client.verified = set(str(index) for index in range(count)) if verified is None else set(verified)
    client.blocked, client.release = threading.Event(), threading.Event()
    client.lookup_calls = []

    def get(path, **kwargs):
        if path.endswith("/Views"):
            return Response({"Items": [{"Name": "fixture-library", "Id": "library"}]})
        if path.endswith("/PlaybackInfo"):
            item_id = path.split("/")[2]
            client.lookup_calls.append(item_id)
            if item_id == block_item:
                client.blocked.set()
                assert client.release.wait(3), "Test did not release blocked source lookup"
            return Response({"MediaSources": [source(item_id)]})
        return Response({"Items": list(client.originals), "TotalRecordCount": count})

    client.get = get
    client._has_normalization_receipt = lambda item: item["Id"] in client.verified
    return client


def paged_client(count, *, include_total=True):
    client = client_fixture(count=count)
    client.page_starts = []

    def get(path, **kwargs):
        if path.endswith("/Views"):
            return Response({"Items": [{"Name": "fixture-library", "Id": "library"},
                                        {"Name": "overlap", "Id": "other"}]})
        params = kwargs["params"]
        assert params["SortBy"] == "SortName"
        assert params["SortOrder"] == "Ascending"
        start = int(params["StartIndex"])
        client.page_starts.append(start)
        # Emulate a server silently clamping our 5,000-item request to 100.
        body = {"Items": client.originals[start:start + 100]}
        if include_total:
            body["TotalRecordCount"] = count
        return Response(body)

    client.get = get
    return client


def test_capped_pages_load_entire_large_library():
    client = paged_client(7503)
    items = client.fetch_items(["fixture-library"], resolve_renditions=False)
    assert {item["Id"] for item in items} == {str(i) for i in range(7503)}
    assert len(items) == 7503
    assert client.page_starts == list(range(0, 7503, 100))


def test_missing_total_still_shuffles_every_item_once_per_cycle():
    client = paged_client(1207, include_total=False)
    items = client.fetch_items(["fixture-library"], resolve_renditions=False)
    assert len(items) == 1207
    assert client.page_starts[-1] == 1207  # terminal empty page
    playlist = PlaylistManager(shuffle=random.Random(42).shuffle)
    playlist.set_source(items)
    expected = {str(i) for i in range(1207)}
    cycles = []
    for _ in range(2):
        first = [playlist.next()["Id"] for _ in range(100)]
        # Background metadata refresh must not reset the shuffle cycle.
        playlist.update_source([dict(item, refreshed=True) for item in items])
        cycle = first + [playlist.next()["Id"] for _ in range(1107)]
        assert len(set(cycle)) == 1207
        assert set(cycle) == expected
        assert any(int(item_id) >= 100 for item_id in first)
        cycles.append(cycle)
    assert cycles[0] != cycles[1]


def test_overlapping_libraries_do_not_weight_shared_items_twice():
    client = paged_client(207)
    items = client.fetch_items(["fixture-library", "overlap"], resolve_renditions=False)
    assert len(items) == len({item["Id"] for item in items}) == 207


def test_repeated_page_is_bounded_and_not_admitted_as_complete():
    client = paged_client(207, include_total=False)
    get = client.get

    def repeated(path, **kwargs):
        if not path.endswith("/Views"):
            kwargs["params"]["StartIndex"] = "0"
        return get(path, **kwargs)

    client.get = repeated
    assert client.fetch_items(["fixture-library"], resolve_renditions=False) == []
    assert len(client.page_starts) == 2


def test_failed_second_page_does_not_admit_a_truncated_library():
    client = paged_client(207)
    get = client.get

    def failed(path, **kwargs):
        response = get(path, **kwargs)
        if not path.endswith("/Views") and int(kwargs["params"]["StartIndex"]) >= 100:
            response.raise_for_status = Mock(side_effect=RuntimeError("HTTP 503"))
        return response

    client.get = failed
    assert client.fetch_items(["fixture-library"], resolve_renditions=False) == []


def loader_fixture(client, startup_cells=8, progressive_start=True, discovery_shuffle=None):
    shuffle = discovery_shuffle if discovery_shuffle is not None else lambda _items: None
    cls = production_class("emby.py", "ContentLoader", {"__init__", "run"},
                           {"os": os, "logger": LOGGER, "random": SimpleNamespace(shuffle=shuffle)})
    loader = cls(client, ["fixture-library"], startup_cells=startup_cells,
                 progressive_start=progressive_start)
    loader.finished, loader.updated, loader.progress = Signal(), Signal(), Signal()
    loader.interrupted = False
    loader.isInterruptionRequested = lambda: loader.interrupted
    loader.errors = []

    def run():
        try:
            loader.run()
        except Exception as exc:
            loader.errors.append(exc)
    loader.worker = threading.Thread(target=run, daemon=True)
    return loader


@contextmanager
def loading(*, prepared_only, count=12, verified=None, block_item=None, startup_cells=8,
            root="/hyperwall/mv", progressive_start=True, discovery_shuffle=None, shared_raw_list=False):
    client = client_fixture(count, verified, block_item)
    if shared_raw_list:
        client.fetch_items = Mock(return_value=client.originals)
    loader = loader_fixture(client, startup_cells, progressive_start, discovery_shuffle)
    with patch.dict(os.environ, {"HYPERWALL_PREPARED_ONLY": "1" if prepared_only else "0",
                                "HYPERWALL_RENDITION_ROOT": root, "HYPERWALL_AUDIO_MODE": "prepared"}):
        loader.worker.start()
        try:
            yield client, loader
        finally:
            client.release.set()
            loader.worker.join(timeout=3)
            assert not loader.worker.is_alive(), "Loader failed to finish after source released"
            assert not loader.errors, repr(loader.errors)


def test_full_library_first_signal_precedes_blocked_rendition_resolution():
    with loading(prepared_only=False, count=906, block_item="0") as (client, loader):
        assert client.blocked.wait(1)
        assert loader.finished.event.is_set(), "Initial playback waited for source resolution"
        first = loader.finished.emitted
        assert len(first) == 1 and len(first[0]) == 906
        assert [item["Id"] for item in first[0]] == [str(i) for i in range(906)]
        assert not loader.updated.emitted
        assert all(not item.get("_hyperwall_prepared") for item in first[0])
    assert len(loader.updated.emitted[-1]) == 906
    assert [item["Id"] for item in loader.updated.emitted[-1]] == [str(i) for i in range(906)]


def test_initial_prepared_scan_shuffles_a_copy_and_preserves_complete_verified_set():
    verified = {str(i) for i in range(40)} - {"3", "33"}
    initial_sets = []
    for permutation in (lambda values: values.reverse(), lambda values: values.sort(key=lambda item: int(item["Id"]))):
        shuffle = Mock(side_effect=permutation)
        with loading(prepared_only=True, count=40, verified=verified, startup_cells=16,
                     discovery_shuffle=shuffle, shared_raw_list=True) as (client, loader):
            pass
        shuffle.assert_called_once()
        assert shuffle.call_args.args[0] is not client.originals
        assert [item["Id"] for item in client.originals] == [str(i) for i in range(40)]
        initial = loader.finished.emitted[0]
        assert len(initial) == 16 and all(item.get("_hyperwall_prepared") is True for item in initial)
        initial_sets.append({item["Id"] for item in initial})
        assert {item["Id"] for item in loader.updated.emitted[-1]} == verified
    assert initial_sets[0] != initial_sets[1]


def test_discovery_shuffle_leaves_refresh_and_mixed_library_order_unchanged():
    for prepared_only, progressive_start in ((True, False), (False, True), (False, False)):
        shuffle = Mock(side_effect=AssertionError("Non-startup/prepared discovery must not shuffle"))
        with loading(prepared_only=prepared_only, progressive_start=progressive_start,
                     discovery_shuffle=shuffle) as (_client, loader):
            pass
        shuffle.assert_not_called()
        assert [item["Id"] for item in loader.finished.emitted[0]] == [str(i) for i in range(12)]


def test_refresh_waits_for_complete_pool_in_both_profiles():
    for prepared_only in (True, False):
        with loading(prepared_only=prepared_only, block_item="9",
                     progressive_start=False) as (client, loader):
            assert client.blocked.wait(1)
            assert not loader.finished.emitted, "Refresh published an incomplete pool"
            assert not loader.updated.emitted
        assert len(loader.finished.emitted) == 1
        assert len(loader.finished.emitted[0]) == 12
        assert all(item.get("_hyperwall_prepared") for item in loader.finished.emitted[0])
        assert not loader.updated.emitted


def test_eight_verified_copies_start_before_remaining_source_lookup_finishes():
    # Item0 is a native alternate without a valid receipt and must not count.
    verified = {str(i) for i in range(1, 12)}
    with loading(prepared_only=True, verified=verified, block_item="9") as (client, loader):
        assert client.blocked.wait(1)
        assert loader.finished.event.is_set(), "Eight verified copies were held behind the remaining library"
        first = loader.finished.emitted
        assert len(first) == 1
        assert [item["Id"] for item in first[0]] == [str(i) for i in range(1, 9)]
        assert all(item.get("_hyperwall_prepared") is True for item in first[0])
        assert not loader.updated.emitted
    final = loader.updated.emitted[-1]
    assert {item["Id"] for item in final} == verified
    assert all(item.get("_hyperwall_prepared") is True for item in final)
    assert len(loader.finished.emitted) == 1


def test_eight_cell_startup_reserves_sixteen_distinct_active_and_prefetch_items():
    class CapturedLoader:
        def __init__(self, client, libraries, startup_cells=1):
            self.startup_cells = startup_cells
            self.finished = self.updated = self.progress = SimpleNamespace(connect=Mock())
            self.start = Mock()

    cls = production_class("wall.py", "WallController", {"_start_async_load"},
                           {"os": os, "ContentLoader": CapturedLoader})
    wall = cls()
    wall.client, wall.libraries, wall.cells = object(), ["fixture-library"], [object() for _ in range(8)]
    wall._on_items_loaded = wall._on_items_updated = wall._on_loading_progress = Mock()
    with patch.dict(os.environ, {"HYPERWALL_RENDITION_ROOT": ""}):
        wall._start_async_load()
    assert wall.loader.startup_cells == 16
    wall.loader.start.assert_called_once()
    with loading(prepared_only=True, count=20, block_item="16",
                 startup_cells=wall.loader.startup_cells) as (client, loader):
        assert client.blocked.wait(1)
        assert len(loader.finished.emitted) == 1
        pool = PlaylistManager(shuffle=lambda _items: None)
        pool.set_source(loader.finished.emitted[0])
        active, prefetched = [], []
        for _ in range(8):
            active.append(pool.next()["Id"])
            prefetched.append(pool.next()["Id"])
        assert len(set(active + prefetched)) == 16


def test_prepared_pool_below_threshold_waits_then_emits_only_verified_items():
    with loading(prepared_only=True, count=5, verified={"1", "3"}, block_item="4") as (client, loader):
        assert client.blocked.wait(1)
        assert not loader.finished.emitted
    assert len(loader.finished.emitted) == 1
    assert [item["Id"] for item in loader.finished.emitted[0]] == ["1", "3"]
    assert all(item.get("_hyperwall_prepared") is True for item in loader.finished.emitted[0])


def test_prepared_only_missing_root_never_admits_originals():
    with loading(prepared_only=True, root="") as (client, loader):
        pass
    assert loader.finished.emitted == [[]]
    assert not client.lookup_calls
    assert not any(loader.updated.emitted)


def test_interruption_during_resolution_does_not_publish_late_update():
    with loading(prepared_only=False, block_item="0") as (client, loader):
        assert client.blocked.wait(1)
        assert loader.finished.event.is_set()
        loader.interrupted = True
    assert not loader.updated.emitted


def test_interruption_during_last_required_receipt_does_not_publish_initial_batch():
    with loading(prepared_only=True, block_item="7", startup_cells=8) as (client, loader):
        assert client.blocked.wait(1)
        assert not loader.finished.emitted
        loader.interrupted = True
    assert not loader.finished.emitted
    assert not loader.updated.emitted


def wall_fixture():
    scheduled = []
    namespace = {name: getattr(constants, name) for name in dir(constants) if name.isupper()}
    namespace.update({"os": os, "logger": LOGGER, "select_playback_candidates": select_playback_candidates,
                      "apply_initial_filter": apply_initial_filter, "DEFAULT_GROUP": DEFAULT_GROUP,
                      "allow_transcode_prefetch": allow_transcode_prefetch,
                      "FAILURE_REASONS": FAILURE_REASONS,
                      "TRANSCODE_PREFETCH_RETRY_S": 1, "TRANSCODE_PREFETCH_RETRY_ATTEMPTS": 3,
                      "QTimer": SimpleNamespace(singleShot=lambda delay, callback: scheduled.append((delay, callback)))})
    cls = production_class("wall.py", "WallController", {
        "_on_items_updated", "_start_empty_cell", "_schedule_transcode_handoff_retry",
        "_schedule_transcode_prefetch_retry", "_queue_problem_transcode", "_on_resource_quarantined",
    }, namespace)
    wall = cls()
    wall._shutdown_requested = wall._cleaned_up = False
    wall._direct_only_pool = wall._normalized_library = False
    wall.all_items = [original(i) for i in range(12)]
    wall.filtered = wall.all_items[:]
    wall.filter_mode = "all"
    wall.playlists = PlaylistManager(shuffle=lambda _items: None)
    wall.playlists.set_source(wall.all_items)
    wall.next_video = Mock(side_effect=AssertionError("Refresh restarted current playback"))
    wall._set_filter = Mock(side_effect=AssertionError("Refresh used restart-producing filter path"))
    wall._local_telemetry = SimpleNamespace(record_event=Mock())
    wall._failure_transcodes = SimpleNamespace(submit=Mock(return_value=False))
    wall._normalization_pending_ids = set()
    wall._starvation_quarantined = set()
    wall.in_outage = lambda: False
    wall.scheduled = scheduled
    wall.cells = [SimpleNamespace(current_item=wall.all_items[0],
                                  _prefetched=(wall.all_items[1], "fixture://queued", "queued-session"),
                                  _emby_session_id="current-session",
                                  play=Mock(side_effect=AssertionError("Refresh reloaded a cell")))]
    return wall


def test_resumed_deduplicated_original_failure_recovers_only_after_verified_refresh():
    wall = wall_fixture()
    failed = wall.all_items[0]
    current, prefetched = wall.cells[0].current_item, wall.cells[0]._prefetched
    # Resumed evidence is already in the worker's dedup set.
    wall._failure_transcodes.submit.return_value = False
    wall._queue_problem_transcode(failed, "malformed_stream")
    wall._on_resource_quarantined(failed)
    wall._failure_transcodes.submit.assert_called_once_with(failed, "malformed_stream")
    assert failed["Id"] in wall._normalization_pending_ids
    assert failed["Id"] in wall._starvation_quarantined
    unverified = [dict(item, _hyperwall_prepared=False) for item in wall.all_items]
    wall._on_items_updated(unverified)
    assert failed["Id"] in wall._starvation_quarantined
    assert failed["Id"] in wall._normalization_pending_ids
    verified = [dict(item, _hyperwall_prepared=True,
                     _hyperwall_media_source_id="prepared-" + item["Id"]) for item in wall.all_items]
    wall._on_items_updated(verified)
    assert failed["Id"] not in wall._starvation_quarantined
    assert failed["Id"] not in wall._normalization_pending_ids
    assert wall.cells[0].current_item is current and wall.cells[0]._prefetched is prefetched
    wall.next_video.assert_not_called()
    wall._local_telemetry.record_event.assert_not_called()  # No duplicate queue event.


def test_prepared_failure_removes_old_recovery_eligibility_and_remains_quarantined():
    for accepted in (False, True):
        wall = wall_fixture()
        failed = wall.all_items[0]
        wall._queue_problem_transcode(failed, "malformed_stream")
        assert failed["Id"] in wall._normalization_pending_ids
        prepared = dict(failed, _hyperwall_prepared=True, _hyperwall_media_source_id="prepared-0")
        wall._failure_transcodes.submit.return_value = accepted
        wall._queue_problem_transcode(prepared, "decoder_recovery_exhausted")
        wall._on_resource_quarantined(prepared)
        assert failed["Id"] not in wall._normalization_pending_ids
        refreshed = [prepared, *wall.all_items[1:]]
        wall._on_items_updated(refreshed)
        assert failed["Id"] in wall._starvation_quarantined
        assert failed["Id"] not in wall._normalization_pending_ids
        wall.next_video.assert_not_called()


def test_unqualified_or_shutdown_failures_cannot_create_recovery_eligibility():
    for condition in ("unknown-reason", "outage", "shutdown", "cleaned-up"):
        wall = wall_fixture()
        wall._shutdown_requested = condition == "shutdown"
        wall._cleaned_up = condition == "cleaned-up"
        wall.in_outage = lambda: condition == "outage"
        reason = "vo_drops" if condition == "unknown-reason" else "malformed_stream"
        wall._queue_problem_transcode(wall.all_items[0], reason)
        wall._failure_transcodes.submit.assert_not_called()
        assert not wall._normalization_pending_ids


def retry_fixture():
    wall = wall_fixture()
    cell = wall.cells[0]
    token = object()
    cell._mpv, cell._mpv_gen, cell._track_generation = object(), 1, 1
    cell._stream_url = "fixture://current"
    cell._prefetch_request_token = None
    cell._current_playback_token = lambda: token
    cell._playback_token_is_current = lambda candidate: candidate is token
    wall._transcode_handoff_retries = {}
    wall._cell_group = lambda _cell: DEFAULT_GROUP
    wall.in_outage = lambda: False
    wall._hand_off = Mock()
    wall._do_prefetch = Mock()
    wall._transcode_load_count = lambda **_kwargs: 999
    wall._auto_transcode_requested = lambda item: not item.get("_hyperwall_prepared")
    return wall, cell, token


def test_handoff_retry_claims_latest_metadata_for_same_reserved_front_id():
    wall, cell, _token = retry_fixture()
    reserved = wall.playlists.peek()
    wall._schedule_transcode_handoff_retry(cell, reserved, force_transcode=False,
                                             preserve_failure_state=False, attempt=1)
    refreshed = [dict(item, _hyperwall_prepared=True, _hyperwall_media_source_id="new-" + item["Id"])
                 for item in wall.all_items]
    wall.playlists.update_source(refreshed)
    wall.scheduled.pop(0)[1]()
    wall._hand_off.assert_called_once()
    assert wall._hand_off.call_args.args[1] is refreshed[0]
    assert wall.playlists.peek() is refreshed[1]
    assert cell.current_item is reserved and "_hyperwall_prepared" not in reserved


def test_prefetch_retry_uses_new_rendition_admission_even_when_transcode_slots_full():
    wall, cell, token = retry_fixture()
    reserved = wall.playlists.peek()
    wall._schedule_transcode_prefetch_retry(cell, token, reserved)
    refreshed = [dict(item, _hyperwall_prepared=True) for item in wall.all_items]
    wall.playlists.update_source(refreshed)
    consumed = []
    wall._do_prefetch = Mock(side_effect=lambda *_args, **_kwargs: consumed.append(wall.playlists.next()))
    wall.scheduled.pop(0)[1]()
    wall._do_prefetch.assert_called_once_with(cell, token, defer_on_saturation=False)
    assert consumed == [refreshed[0]] and consumed[0] is refreshed[0]
    assert not wall.scheduled  # New direct source needed no transcode retry.


def test_refreshed_retry_cannot_claim_an_item_consumed_by_another_cell():
    for kind in ("handoff", "prefetch"):
        wall, cell, token = retry_fixture()
        reserved = wall.playlists.peek()
        if kind == "handoff":
            wall._schedule_transcode_handoff_retry(cell, reserved, force_transcode=False,
                                                     preserve_failure_state=False, attempt=1)
        else:
            wall._schedule_transcode_prefetch_retry(cell, token, reserved)
        refreshed = [dict(item, _hyperwall_prepared=True) for item in wall.all_items]
        wall.playlists.update_source(refreshed)
        assert wall.playlists.next() is refreshed[0]  # Another cell wins the reservation.
        wall.scheduled.pop(0)[1]()
        wall._hand_off.assert_not_called()
        wall._do_prefetch.assert_not_called()
        assert wall.playlists.peek() is refreshed[1]


def test_requeue_after_refresh_uses_current_metadata_without_mutating_reserved_item():
    pool = PlaylistManager(shuffle=lambda _items: None)
    old = [original(i) for i in range(3)]
    pool.set_source(old)
    reserved = pool.next()
    refreshed = [dict(item, _hyperwall_prepared=True) for item in old]
    pool.update_source(refreshed)
    pool.push_front(DEFAULT_GROUP, reserved)
    assert pool.next() is refreshed[0]
    assert "_hyperwall_prepared" not in reserved
    pool.push_front(DEFAULT_GROUP, {"Id": "removed"})
    assert pool.peek() is refreshed[1]


def test_updated_pool_preserves_active_cell_prefetch_and_full_library_identity():
    wall = wall_fixture()
    cell = wall.cells[0]
    current, prefetched = cell.current_item, cell._prefetched
    enriched = [dict(item, _hyperwall_prepared=True, _hyperwall_media_source_id="prepared-" + item["Id"])
                for item in wall.all_items]
    wall._on_items_updated(enriched)
    assert wall.all_items == enriched and wall.filtered == enriched
    assert wall.playlists.pool_size() == len(enriched)
    assert wall.playlists.peek() is enriched[0]
    assert cell.current_item is current and cell._prefetched is prefetched
    assert cell._emby_session_id == "current-session"
    cell.play.assert_not_called()
    wall.next_video.assert_not_called()


def test_updated_pool_preserves_current_favorites_filter():
    wall = wall_fixture()
    wall.filter_mode = "favorites"
    wall._on_items_updated([dict(item, _hyperwall_prepared=True) for item in wall.all_items])
    assert len(wall.all_items) == 12
    assert [item["Id"] for item in wall.filtered] == [str(i) for i in range(0, 12, 2)]
    assert wall.playlists.pool_size() == 6


def test_refresh_preserves_explicit_direct_and_normalized_admission():
    for policy_flag in ("_direct_only_pool", "_normalized_library"):
        wall = wall_fixture()
        setattr(wall, policy_flag, True)
        safe_source = source("safe")
        safe_source["MediaStreams"][0].update(AverageFrameRate=1, BitRate=1_000_000)
        safe = {"Id": "safe", "MediaSources": [safe_source]}
        heavy_source = source("heavy")
        heavy_source["MediaStreams"][0].update(AverageFrameRate=1000, BitRate=1_000_000_000_000)
        heavy = {"Id": "heavy", "MediaSources": [heavy_source]}
        unmeasured = {"Id": "unmeasured", "MediaSources": [], "MediaStreams": []}
        current = wall.cells[0].current_item
        wall._on_items_updated([safe, heavy, unmeasured])
        assert wall.all_items == wall.filtered == [safe], policy_flag
        assert wall.playlists.pool_size() == 1
        assert wall.playlists.peek() is safe
        assert wall.cells[0].current_item is current
        wall.next_video.assert_not_called()


def test_refresh_preserves_explicit_heavy_item_soak_exception():
    wall = wall_fixture()
    wall._direct_only_pool = True
    heavy_source = source("heavy")
    heavy_source["MediaStreams"][0].update(AverageFrameRate=120, BitRate=1_000_000_000)
    heavy = {"Id": "heavy", "MediaSources": [heavy_source]}
    with patch.dict(os.environ, {"HYPERWALL_SOAK_ACTIVE": "1", "HYPERWALL_SOAK_ITEM_ID": "heavy"}):
        wall._on_items_updated([heavy])
        assert wall.filtered == [heavy] and wall.filter_mode == "item"
        wall._normalized_library = True
        wall._on_items_updated([heavy])
        assert wall.filtered == [] and wall.filter_mode == "item-not-found"


def test_refresh_starts_only_empty_cell_and_stale_stagger_callbacks_do_not_reload():
    wall = wall_fixture()
    active = wall.cells[0]
    empty = SimpleNamespace(current_item=None, _closing=False)
    wall.cells.append(empty)
    updated = [dict(item, _hyperwall_prepared=True) for item in wall.all_items]
    wall._on_items_updated(updated)
    wall._on_items_updated(updated)
    assert len(wall.scheduled) == 2
    wall.next_video = Mock(side_effect=lambda cell, _force: setattr(cell, "current_item", updated[0]))
    for _delay, callback in wall.scheduled:
        callback()
    wall.next_video.assert_called_once_with(empty, False)
    assert active.current_item is not updated[0]


def test_playlist_refresh_preserves_unconsumed_cycle_and_appends_new_ids():
    playlist = PlaylistManager(shuffle=lambda _items: None)
    old = [original(i) for i in range(6)]
    playlist.set_source(old)
    playlist.set_source([original(99)], "other-display")
    assert playlist.next() is old[0]  # Current playback.
    assert playlist.next() is old[1]  # Already reserved prefetch.
    refreshed = [dict(original(i), _hyperwall_prepared=True) for i in (0, 1, 2, 4, 5, 6, 7)]
    playlist.update_source(refreshed)
    consumed = [playlist.next() for _ in range(5)]
    assert [item["Id"] for item in consumed] == ["2", "4", "5", "6", "7"]
    assert all(any(item is updated for updated in refreshed) for item in consumed)
    assert playlist.pool_size() == 7
    assert playlist.next("other-display")["Id"] == "99"
    assert playlist.next()["Id"] == "0"  # A fresh cycle may now repeat.


def test_shutdown_ignores_late_library_refresh():
    for flag in ("_shutdown_requested", "_cleaned_up"):
        wall = wall_fixture()
        previous = wall.all_items
        setattr(wall, flag, True)
        wall._on_items_updated([dict(item, _hyperwall_prepared=True) for item in previous])
        assert wall.all_items is previous
        assert wall.playlists.peek() is previous[0]
        wall.next_video.assert_not_called()


def run_all():
    failed = 0
    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc!r}")
    print(f"  {len(tests)-failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(run_all())
