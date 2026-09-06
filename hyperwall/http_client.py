"""Small stdlib-only JSON HTTP client used by the macOS Emby path."""
from __future__ import annotations

import json as jsonlib
import ssl
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class HttpRequestError(OSError):
    """Transport failure before an HTTP response was available."""


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    content: bytes
    headers: Mapping[str, str]

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return jsonlib.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HttpRequestError(f"HTTP status {self.status_code}")


class JsonHttpSession:
    """Small stdlib-only JSON session with the response shape EmbyClient needs."""

    def __init__(
        self,
        *,
        verify_ssl: bool = True,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.headers = dict(headers or {})
        self._context = (
            ssl.create_default_context()
            if verify_ssl
            else ssl._create_unverified_context()
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        timeout: float = 30,
    ) -> HttpResponse:
        if params:
            parts = urlsplit(url)
            query = urlencode(params, doseq=True)
            url = urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

        request_headers = dict(self.headers)
        request_headers.update(headers or {})
        data = None
        if json is not None:
            data = jsonlib.dumps(json).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")

        request = Request(
            url,
            data=data,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            with urlopen(request, timeout=timeout, context=self._context) as response:
                return HttpResponse(
                    status_code=int(response.status),
                    content=response.read(),
                    headers=dict(response.headers.items()),
                )
        except HTTPError as exc:
            return HttpResponse(
                status_code=int(exc.code),
                content=exc.read(),
                headers=dict(exc.headers.items()),
            )
        except (URLError, OSError, TimeoutError) as exc:
            raise HttpRequestError(str(exc)) from exc

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("POST", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("DELETE", url, **kwargs)

    def close(self) -> None:
        """Match the session lifecycle API; urllib has no persistent handle here."""


__all__ = ["HttpRequestError", "HttpResponse", "JsonHttpSession"]
