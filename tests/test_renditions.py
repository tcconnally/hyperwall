"""Prepared sources retain library identity and require explicit provenance."""

from __future__ import annotations

from copy import deepcopy
import os
import sys
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperwall.renditions import NORMALIZATION_RECEIPT, explicit_receipt_range, prefer_rendition, valid_receipt_response
from hyperwall.playback_plan import plan_playback
from hyperwall.urls import build_stream_url_for_plan


def _source(**changes):
    source = {
        "Id": "prepared-source", "Path": "/hyperwall/mv/nested/clip.mp4",
        "Protocol": "File", "Container": "mp4", "SupportsDirectStream": True,
        "Size": 1000, "RunTimeTicks": 100_000_000,
        "MediaStreams": [{"Type": "Video", "Codec": "h264", "AverageFrameRate": 30,
                          "BitRate": 8_000_000, "Width": 1920, "Height": 1080}],
    }
    source.update(changes)
    return source


def _item(*sources):
    return {"Id": "original-item", "Name": "Original title", "Path": "/media/etc/mv/clip.mkv",
            "UserData": {"IsFavorite": True}, "TagItems": [{"Name": "selected"}],
            "MediaSources": list(sources)}


def test_selection_preserves_identity_metadata_and_input():
    original = _item(_source(Path="/media/etc/mv/clip.mkv"), _source())
    before = deepcopy(original)
    selected = prefer_rendition(original, "/hyperwall/mv")
    assert selected is not original
    assert original == before
    for key in ("Id", "Name", "Path", "UserData", "TagItems"):
        assert selected[key] == original[key]
    assert selected["_hyperwall_prepared"] is False
    assert selected["_hyperwall_media_source_id"] == "prepared-source"
    assert len(selected["MediaSources"]) == 1


def test_missing_renditions_preserve_all_originals():
    items = [_item(_source()), _item(_source(Path="/media/etc/mv/a.mp4")), _item()]
    selected = [prefer_rendition(item, "/hyperwall/mv") for item in items]
    assert len(selected) == len(items)
    assert selected[1] is items[1] and selected[2] is items[2]
    assert sum(bool(item.get("_hyperwall_media_source_id")) for item in selected) == 1
    assert not any(item.get("_hyperwall_prepared") for item in selected)


def test_unconfigured_root_never_arms_prepared_audio():
    item = _item(_source())
    assert prefer_rendition(item, None) is item
    assert prefer_rendition(item, "") is item


def test_path_boundary_and_traversal_are_rejected():
    for path in ("/hyperwall/mv-other/clip.mp4", "/hyperwall/mv/../clip.mp4",
                 "/hyperwall/mv", "relative/clip.mp4", "https://host/hyperwall/mv/a.mp4",
                 "/hyperwall/mv/a\\b.mp4", "/hyperwall/mv/clip.partial.mp4.tmp"):
        item = _item(_source(Path=path))
        assert prefer_rendition(item, "/hyperwall/mv") is item, path
    item = _item(_source())
    for root in ("/", "relative", "/hyperwall/../hyperwall/mv"):
        assert prefer_rendition(item, root) is item, root


def test_incomplete_or_nonlocal_sources_are_rejected():
    changes = [{"Size": 0}, {"RunTimeTicks": None}, {"Size": True}, {"Id": ""},
               {"Protocol": "Http"}, {"SupportsDirectStream": False},
               {"RequiresOpening": True}, {"IsInfiniteStream": True},
               {"IsRemote": True}, {"Container": "mkv"}, {"MediaStreams": []}]
    for change in changes:
        item = _item(_source(**change))
        assert prefer_rendition(item, "/hyperwall/mv") is item, change


def test_ambiguous_renditions_do_not_select_arbitrarily():
    item = _item(_source(), _source(Id="second", Path="/hyperwall/mv/other.mp4"))
    assert prefer_rendition(item, "/hyperwall/mv") is item


def test_plan_and_url_use_selected_source_but_original_item():
    item = prefer_rendition(_item(_source(Id="id with/&?")), "/hyperwall/mv")
    plan = plan_playback(item)
    assert plan.server_mode == "direct"
    assert plan.source_bitrate_mbps == 8
    assert plan.item_id == "original-item"
    url = build_stream_url_for_plan(
        base="http://emby", item_id=item["Id"], api_key="key", session_id="session",
        plan=plan, media_source_id=item["_hyperwall_media_source_id"],
    )
    assert urlsplit(url).path == "/Videos/original-item/stream"
    query = parse_qs(urlsplit(url).query)
    assert query["MediaSourceId"] == ["id with/&?"]
    assert query["PlaySessionId"] == ["session"]
    assert query["static"] == ["true"]


def test_loader_resolves_synced_sources_absent_from_library_response():
    try:
        from hyperwall.emby import EmbyClient
    except ImportError:
        print("SKIP loader integration requires PyQt6/requests")
        return
    original = _item(_source(Path="/media/etc/mv/clip.mp4"))
    client = EmbyClient("http://unused", "user", "password")
    client.user_id = "user"
    calls = []

    class Response:
        def __init__(self, body):
            self.body = body
        def json(self):
            return self.body
        def raise_for_status(self):
            pass

    def get(path, **kwargs):
        calls.append(path)
        if path.endswith("/Views"):
            return Response({"Items": [{"Name": "mv", "Id": "library"}]})
        if path.endswith("/PlaybackInfo"):
            return Response({"MediaSources": [*original["MediaSources"], _source()]})
        return Response({"Items": [original], "TotalRecordCount": 1})

    client.get = get
    with patch.dict(os.environ, {"HYPERWALL_RENDITION_ROOT": "/hyperwall/mv",
                                "HYPERWALL_AUDIO_MODE": "lazy", "HYPERWALL_PREPARED_ONLY": "0"}):
        items = client.fetch_items(["mv"])
    assert len(items) == 1 and items[0]["Id"] == original["Id"]
    assert items[0]["_hyperwall_prepared"] is False
    assert items[0]["UserData"] == original["UserData"]
    assert any(path.endswith("/PlaybackInfo") for path in calls)
    calls.clear()
    with patch.dict(os.environ, {"HYPERWALL_RENDITION_ROOT": ""}):
        assert client.fetch_items(["mv"])[0] is original
    assert not any(path.endswith("/PlaybackInfo") for path in calls)
    client.close()


def test_receipt_requires_exact_suffix_bytes_and_range():
    assert len(NORMALIZATION_RECEIPT) == 64
    assert NORMALIZATION_RECEIPT[:8] == b"\x00\x00\x00@free"
    assert valid_receipt_response(206, "bytes 936-999/1000", NORMALIZATION_RECEIPT)
    for status, content_range, body in (
        (200, "bytes 936-999/1000", NORMALIZATION_RECEIPT),
        (206, "bytes 0-63/1000", NORMALIZATION_RECEIPT),
        (206, "bytes 0-63/64", NORMALIZATION_RECEIPT),
        (206, "bytes 936-999/*", NORMALIZATION_RECEIPT),
        (206, "bytes 936-999/1000", b"x" * 64),
        (206, "bytes 936-999/1000", NORMALIZATION_RECEIPT + b"x"),
        (206, "bytes 936-999/1000", NORMALIZATION_RECEIPT[:-1]),
    ):
        assert not valid_receipt_response(status, content_range, body)
    assert explicit_receipt_range(206, "bytes 0-64/1000") == "bytes=936-999"
    for status, header in ((200, "bytes 0-64/1000"), (206, "bytes 0-64/*"),
                           (206, "bytes 0-64/65"), (206, "bytes 0-63/1000")):
        assert explicit_receipt_range(status, header) is None


def test_receipt_http_read_is_bounded_authenticated_and_rejects_full_video():
    try:
        from hyperwall.emby import EmbyClient
        import urllib3
    except ImportError:
        print("SKIP receipt HTTP integration requires PyQt6/requests")
        return
    client = EmbyClient("http://unused", "user", "password")
    client.access_token = "private-test-token"
    item = prefer_rendition(_item(_source()), "/hyperwall/mv")

    class Response:
        def __init__(self, status=206, content_range="bytes 936-999/1000", body=NORMALIZATION_RECEIPT):
            self.status_code = status
            self.headers = {"Content-Range": content_range, "Content-Length": "64"}
            self.body, self.reads, self.closed, self.raw = body, [], False, self
        def read(self, amount, *, decode_content):
            self.reads.append((amount, decode_content))
            return self.body
        def close(self):
            self.closed = True

    calls = []
    response = Response()
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response
    client._session.get = get
    assert client._has_normalization_receipt(item)
    assert response.reads == [(65, False)] and response.closed
    url, kwargs = calls[-1]
    assert "private-test-token" not in url and "api_key" not in url
    assert kwargs["headers"]["X-Emby-Token"] == "private-test-token"
    assert kwargs["headers"]["Range"] == "bytes=-64"
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
    assert kwargs["params"]["MediaSourceId"] == "prepared-source"
    for response in (Response(status=200), Response(status=302), Response(content_range="bytes 0-63/1000")):
        assert not client._has_normalization_receipt(item)
        assert not response.reads and response.closed
    response = Response(body=b"no receipt".ljust(64, b"\0"))
    assert not client._has_normalization_receipt(item)
    # Greg's Emby interprets the suffix as an inclusive first-65-byte range.
    prefix = Response(content_range="bytes 0-64/1000")
    prefix.headers["Content-Length"] = "65"
    tail = Response()
    def emby_get(url, **kwargs):
        calls.append((url, kwargs))
        return prefix if kwargs["headers"]["Range"] == "bytes=-64" else tail
    client._session.get = emby_get
    before = len(calls)
    assert client._has_normalization_receipt(item)
    assert len(calls) - before == 2 and not prefix.reads and prefix.closed
    assert calls[-1][1]["headers"]["Range"] == "bytes=936-999"
    assert tail.reads == [(65, False)] and tail.closed
    tail = Response(content_range="bytes 0-64/1000")
    tail.headers["Content-Length"] = "65"
    before = len(calls)
    assert not client._has_normalization_receipt(item)
    assert len(calls) - before == 2 and not tail.reads
    client._session.get = get
    response = Response()
    def timeout_read(*args, **kwargs):
        raise urllib3.exceptions.ReadTimeoutError(None, "<redacted>", "timeout")
    response.read = timeout_read
    assert not client._has_normalization_receipt(item) and response.closed
    client.close()


def test_prepared_only_is_explicit_and_missing_receipts_are_not_prepared():
    try:
        from hyperwall.emby import EmbyClient
    except ImportError:
        print("SKIP prepared-only integration requires PyQt6/requests")
        return
    client = EmbyClient("http://unused", "user", "password")
    client.user_id = "user"
    originals = [_item(_source()), _item(_source()), _item()]
    for index, item in enumerate(originals):
        item["Id"] = str(index)
    class Response:
        def __init__(self, body): self.body = body
        def json(self): return self.body
        def raise_for_status(self): pass
    def get(path, **kwargs):
        if path.endswith("/Views"):
            return Response({"Items": [{"Name": "mv", "Id": "library"}]})
        if path.endswith("/PlaybackInfo"):
            return Response({"MediaSources": []})
        return Response({"Items": originals, "TotalRecordCount": len(originals)})
    client.get = get
    receipt_checks = []
    def receipt(item):
        receipt_checks.append(item["Id"])
        return item["Id"] == "0"
    client._has_normalization_receipt = receipt
    with patch.dict(os.environ, {"HYPERWALL_RENDITION_ROOT": "/hyperwall/mv",
                                "HYPERWALL_AUDIO_MODE": "prepared", "HYPERWALL_PREPARED_ONLY": "0"}):
        all_items = client.fetch_items(["mv"])
        assert len(all_items) == 3
        assert all_items[0]["_hyperwall_prepared"] is True
        assert all_items[1]["_hyperwall_prepared"] is False
        assert all_items[1]["_hyperwall_media_source_id"] == "prepared-source"
        assert all_items[2] is originals[2]
        with patch.dict(os.environ, {"HYPERWALL_PREPARED_ONLY": "1", "HYPERWALL_AUDIO_MODE": "lazy"}):
            ready = client.fetch_items(["mv"])
            assert [item["Id"] for item in ready] == ["0"]
            assert ready[0]["UserData"] == originals[0]["UserData"]
        with patch.dict(os.environ, {"HYPERWALL_PREPARED_ONLY": "1", "HYPERWALL_RENDITION_ROOT": ""}):
            assert client.fetch_items(["mv"]) == []
    assert receipt_checks
    client.close()


def test_lookup_outage_preserves_every_original_and_stops_retrying():
    try:
        from hyperwall.emby import EmbyClient
        import requests
    except ImportError:
        print("SKIP loader integration requires PyQt6/requests")
        return
    client = EmbyClient("http://unused", "user", "password")
    calls = []
    originals = [_item() for _ in range(10)]

    def get(path, **kwargs):
        calls.append(path)
        raise requests.Timeout()

    client.get = get
    selected = client._prefer_renditions(originals, "/hyperwall/mv")
    assert len(calls) == 3
    assert len(selected) == len(originals)
    assert all(selected[index] is item for index, item in enumerate(originals))
    client.close()


def run_all():
    failures = 0
    for name, test in list(globals().items()):
        if name.startswith("test_") and callable(test):
            try:
                test()
                print("PASS", name)
            except Exception as exc:
                failures += 1
                print("FAIL", name, repr(exc))
    return failures


if __name__ == "__main__":
    raise SystemExit(run_all())
