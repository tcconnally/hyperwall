"""Tests for the bounded Ollama model unload control."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hyperwall.ollama_control import (  # noqa: E402
    OllamaControlError,
    loaded_models,
    unload_loaded_models,
)


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds: float):
        self.now += seconds


def test_loaded_models_reads_only_current_residency():
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, request.method, timeout))
        return _Response({"models": [{"name": "qwen3.5:9b-hermes"}]})

    assert loaded_models("http://ollama:11434", opener=opener) == [
        "qwen3.5:9b-hermes"
    ]
    assert calls == [("http://ollama:11434/api/ps", "GET", 5.0)]


def test_unload_loaded_models_verifies_empty_residency():
    state = {"models": [{"name": "qwen3.5:9b-hermes"}]}
    calls = []
    clock = _Clock()

    def opener(request, timeout):
        calls.append((request.full_url, request.method))
        if request.method == "POST":
            state["models"] = []
            return _Response({"done": True})
        return _Response(dict(state))

    assert unload_loaded_models(
        "http://ollama:11434",
        wait_s=2.0,
        opener=opener,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    ) == []
    assert calls == [
        ("http://ollama:11434/api/ps", "GET"),
        ("http://ollama:11434/api/generate", "POST"),
        ("http://ollama:11434/api/ps", "GET"),
    ]


def test_unload_loaded_models_reports_models_that_will_not_leave():
    clock = _Clock()

    def opener(request, timeout):
        if request.method == "POST":
            return _Response({"done": True})
        return _Response({"models": [{"name": "stuck"}]})

    assert unload_loaded_models(
        "http://ollama:11434",
        wait_s=1.0,
        opener=opener,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    ) == ["stuck"]


def test_loaded_models_rejects_malformed_response():
    def opener(request, timeout):
        return _Response({"models": [{"name": ""}]})

    try:
        loaded_models("http://ollama:11434", opener=opener)
    except OllamaControlError as exc:
        assert "model name" in str(exc)
    else:
        raise AssertionError("malformed residency must fail closed")


def run_all() -> int:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print("PASS", test.__name__)
        except Exception as exc:
            failed += 1
            print("FAIL", test.__name__, exc)
    print(f"{len(tests) - failed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    raise SystemExit(run_all())
