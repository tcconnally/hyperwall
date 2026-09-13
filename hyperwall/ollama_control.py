"""Small, fail-closed control surface for a local Ollama daemon."""
from __future__ import annotations

import json
import time
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen


class OllamaControlError(RuntimeError):
    """The daemon did not return a trustworthy control response."""


Opener = Callable[..., Any]


def _endpoint(base_url: str, path: str) -> str:
    base = base_url.strip().rstrip("/")
    if not base:
        raise OllamaControlError("Ollama URL is empty")
    return base + path


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout_s: float = 5.0,
    opener: Opener = urlopen,
) -> dict[str, Any]:
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        _endpoint(base_url, path),
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with opener(request, timeout=timeout_s) as response:
            raw = response.read()
    except (OSError, URLError, TimeoutError) as exc:
        raise OllamaControlError(f"Ollama {method} {path} failed: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OllamaControlError(f"Ollama {method} {path} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise OllamaControlError(f"Ollama {method} {path} returned a non-object")
    return value


def loaded_models(
    base_url: str,
    *,
    timeout_s: float = 5.0,
    opener: Opener = urlopen,
) -> list[str]:
    """Return the currently resident model names from `/api/ps`."""
    payload = _request_json(
        base_url, "/api/ps", timeout_s=timeout_s, opener=opener,
    )
    raw_models = payload.get("models")
    if raw_models is None:
        return []
    if not isinstance(raw_models, list):
        raise OllamaControlError("Ollama /api/ps models is not a list")
    names: list[str] = []
    for entry in raw_models:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise OllamaControlError("Ollama /api/ps has a malformed model name")
        name = entry["name"].strip()
        if not name:
            raise OllamaControlError("Ollama /api/ps has an empty model name")
        if name not in names:
            names.append(name)
    return names


def _unload_one(
    base_url: str,
    model: str,
    *,
    timeout_s: float,
    opener: Opener,
) -> None:
    _request_json(
        base_url,
        "/api/generate",
        method="POST",
        payload={
            "model": model,
            "prompt": "",
            "stream": False,
            "keep_alive": 0,
        },
        timeout_s=timeout_s,
        opener=opener,
    )


def unload_loaded_models(
    base_url: str,
    *,
    wait_s: float = 20.0,
    timeout_s: float = 5.0,
    opener: Opener = urlopen,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Unload every resident model and return any names still resident.

    The final `/api/ps` read is intentional: callers must not treat a
    successful unload request as proof that VRAM was released.
    """
    if wait_s < 0:
        raise OllamaControlError("wait_s must be nonnegative")
    initial = loaded_models(base_url, timeout_s=timeout_s, opener=opener)
    for model in initial:
        _unload_one(
            base_url, model, timeout_s=timeout_s, opener=opener,
        )
    deadline = monotonic() + wait_s
    remaining = loaded_models(base_url, timeout_s=timeout_s, opener=opener)
    while remaining and monotonic() < deadline:
        sleep(min(0.25, max(0.0, deadline - monotonic())))
        remaining = loaded_models(base_url, timeout_s=timeout_s, opener=opener)
    return remaining
