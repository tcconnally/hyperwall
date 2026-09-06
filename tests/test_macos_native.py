"""Mac-native architecture contracts that do not require Qt or libmpv."""
from __future__ import annotations

import json
import os
import sys
from unittest import mock
from urllib.request import Request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def test_macos_options_use_one_render_path_and_explicit_decoder_profile():
    from hyperwall.constants import macos_mpv_opts

    safe_16 = macos_mpv_opts(profile="safe", physical_memory_mb=16 * 1024)
    safe_64 = macos_mpv_opts(profile="safe", physical_memory_mb=64 * 1024)
    hardware_copy = macos_mpv_opts(profile="hardware-copy", physical_memory_mb=16 * 1024)

    assert safe_16["vo"] == "libmpv"
    assert safe_16["ao"].startswith("coreaudio")
    assert "gpu_api" not in safe_16
    assert safe_16["hwdec"] == "no"
    assert safe_64["hwdec"] == "no", "RAM must not silently select a decoder"
    assert hardware_copy["hwdec"] == "videotoolbox-copy"


def test_macos_runtime_rejects_non_macos_hosts():
    from hyperwall.macos_runtime import require_macos

    require_macos("darwin")
    try:
        require_macos("linux")
    except RuntimeError as exc:
        assert "macOS" in str(exc)
    else:
        raise AssertionError("non-macOS runtime must fail closed")


def test_stdlib_http_session_builds_json_request_and_response():
    from hyperwall.http_client import JsonHttpSession

    class _Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    calls: list[tuple[Request, float, object]] = []

    def fake_urlopen(request, timeout, context):
        calls.append((request, timeout, context))
        return _Response()

    with mock.patch("hyperwall.http_client.urlopen", fake_urlopen):
        response = JsonHttpSession(
            verify_ssl=False,
            headers={"User-Agent": "HyperWall/test"},
        ).post(
            "https://emby.example/Users/AuthenticateByName",
            headers={"X-Emby-Authorization": "redacted"},
            json={"Username": "u", "Pw": "p"},
            timeout=7,
        )

    request, timeout, context = calls[0]
    assert request.method == "POST"
    assert request.full_url.endswith("/Users/AuthenticateByName")
    assert request.headers["User-agent"] == "HyperWall/test"
    assert request.headers["X-emby-authorization"] == "redacted"
    assert json.loads(request.data.decode("utf-8")) == {"Username": "u", "Pw": "p"}
    assert timeout == 7
    assert context is not None
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def run_all() -> int:
    tests = [
        test_macos_options_use_one_render_path_and_explicit_decoder_profile,
        test_macos_runtime_rejects_non_macos_hosts,
        test_stdlib_http_session_builds_json_request_and_response,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    raise SystemExit(run_all())
