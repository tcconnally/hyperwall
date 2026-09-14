#!/usr/bin/env python3
"""Run the Linux disconnect benchmark against a real configured Emby item.

The Pop!_OS host does not mount Greg's media tree. This wrapper authenticates
with the existing config.ini, selects an item from the configured Emby library,
checks the normalized wall-safe contract, and passes an authenticated direct
stream URL to the existing display-independent benchmark. Tokens are never
printed or written to reports; the child benchmark redacts its source URL.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hyperwall.emby_benchmark import wall_safe_violations  # noqa: E402
from hyperwall.urls import build_stream_url  # noqa: E402


class _HttpResponse:
    def __init__(self, status_code: int, body: bytes):
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return json.loads(self._body.decode("utf-8"))

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")


class _EmbySession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        timeout: float,
    ) -> _HttpResponse:
        if params:
            url = f"{url}?{urlencode(params)}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = dict(self.headers)
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                return _HttpResponse(response.status, response.read())
        except HTTPError as exc:
            return _HttpResponse(exc.code, exc.read())
        except URLError as exc:
            raise OSError(str(exc.reason)) from exc

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        timeout: float,
    ) -> _HttpResponse:
        return self._request("GET", url, params=params, timeout=timeout)

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_payload: dict[str, Any] | None = None,
        timeout: float,
    ) -> _HttpResponse:
        original_headers = self.headers
        if headers:
            self.headers = {**self.headers, **headers}
        try:
            return self._request("POST", url, payload=json_payload, timeout=timeout)
        finally:
            self.headers = original_headers

    def close(self) -> None:
        return None


class EmbyBenchmarkError(RuntimeError):
    """A user-actionable configuration, authentication, or selection error."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Linux KVM benchmark against an authenticated Emby item."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config.ini",
        help="HyperWall config.ini (default: repository config.ini).",
    )
    parser.add_argument(
        "--library",
        default=None,
        help="Emby library name (default: first last_libraries entry in config.ini).",
    )
    listing = parser.add_mutually_exclusive_group()
    listing.add_argument(
        "--list-items",
        action="store_true",
        help="List every configured-library item with metadata and wall-safe diagnostics; do not benchmark.",
    )
    listing.add_argument(
        "--list-wall-safe",
        action="store_true",
        help="Explicitly list only items satisfying the normalized wall-safe contract.",
    )
    parser.add_argument(
        "--item-id",
        help="Emby item ID to benchmark; required unless listing items.",
    )
    parser.add_argument("--output", type=Path, help="Benchmark report directory.")
    parser.add_argument("--cells", type=int, default=8)
    parser.add_argument("--duration-s", type=float, default=120.0)
    parser.add_argument("--poll-s", type=float, default=2.0)
    parser.add_argument("--mpv", default="mpv")
    parser.add_argument("--hwdec", default="auto-safe")
    parser.add_argument("--nvidia-smi", default="nvidia-smi")
    parser.add_argument("--drm-root", type=Path, default=None)
    parser.add_argument("--keep-awake", action="store_true")
    parser.add_argument("--require-disconnect", action="store_true")
    return parser


def _load_login(config_path: Path) -> tuple[str, str, str, str]:
    parser = configparser.ConfigParser()
    if not config_path.is_file():
        raise EmbyBenchmarkError(f"config file does not exist: {config_path}")
    parser.read(config_path, encoding="utf-8")
    if "Login" not in parser:
        raise EmbyBenchmarkError("config.ini has no [Login] section")
    login = parser["Login"]
    server_url = login.get("server_url", "").strip().rstrip("/")
    username = login.get("username", "").strip()
    password = login.get("password", "")
    if not server_url or not username or not password:
        raise EmbyBenchmarkError(
            "config.ini [Login] must contain server_url, username, and password"
        )
    configured_libraries = parser.get(
        "Settings", "last_libraries", fallback=""
    ).split(",")
    library = next((value.strip() for value in configured_libraries if value.strip()), "")
    return server_url, username, password, library


def _authenticate(
    server_url: str, username: str, password: str,
) -> tuple[_EmbySession, str, str]:
    session = _EmbySession()
    device_id = f"hyperwall-linux-benchmark-{uuid.uuid4().hex[:12]}"
    response: _HttpResponse | None = None
    try:
        response = session.post(
            f"{server_url}/Users/AuthenticateByName",
            headers={
                "X-Emby-Authorization": (
                    'MediaBrowser Client="HyperWall", Device="PC", '
                    f'DeviceId="{device_id}", Version="10.15.0"'
                ),
            },
            json_payload={"Username": username, "Pw": password},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        token = payload["AccessToken"]
        user_id = payload["User"]["Id"]
    except (OSError, ValueError, KeyError) as exc:
        status = response.status_code if response is not None else "network"
        raise EmbyBenchmarkError(
            f"Emby authentication failed ({status})"
        ) from exc
    session.headers.update({"X-Emby-Token": token})
    return session, user_id, token


def _library_items(
    session: _EmbySession, server_url: str, user_id: str, library: str,
) -> list[dict[str, Any]]:
    views_response = session.get(
        f"{server_url}/Users/{user_id}/Views", timeout=10,
    )
    try:
        views_response.raise_for_status()
        views = views_response.json().get("Items", [])
    except (OSError, ValueError) as exc:
        raise EmbyBenchmarkError("could not read Emby libraries") from exc
    view = next(
        (item for item in views if str(item.get("Name", "")).casefold() == library.casefold()),
        None,
    )
    if view is None:
        available = sorted(str(item.get("Name", "")) for item in views)
        raise EmbyBenchmarkError(
            f"Emby library not found: {library!r}; available={available}"
        )
    parent_id = view.get("Id")
    if not parent_id:
        raise EmbyBenchmarkError(f"Emby library has no ID: {library!r}")

    items: list[dict[str, Any]] = []
    page_size = 5_000
    while True:
        response = session.get(
            f"{server_url}/Users/{user_id}/Items",
            params={
                "ParentId": parent_id,
                "Recursive": "true",
                "IncludeItemTypes": "Video,MusicVideo,Movie,Episode",
                "Fields": "MediaSources,MediaStreams",
                "StartIndex": str(len(items)),
                "Limit": str(page_size),
            },
            timeout=30,
        )
        try:
            response.raise_for_status()
            payload = response.json()
        except (OSError, ValueError) as exc:
            raise EmbyBenchmarkError(f"could not read items from library: {library!r}") from exc
        batch = payload.get("Items", [])
        if not isinstance(batch, list):
            raise EmbyBenchmarkError("Emby returned an invalid item list")
        items.extend(item for item in batch if isinstance(item, dict))
        total = payload.get("TotalRecordCount", len(items))
        if not batch or len(items) >= total:
            return items


def _item_summary(item: dict[str, Any], violations: list[str]) -> dict[str, Any]:
    source = (item.get("MediaSources") or [{}])[0]
    streams = source.get("MediaStreams") or item.get("MediaStreams") or []
    video = next((s for s in streams if s.get("Type") == "Video"), {})
    audio = next((s for s in streams if s.get("Type") == "Audio"), {})
    return {
        "id": item.get("Id"),
        "name": item.get("Name"),
        "container": source.get("Container"),
        "video_codec": video.get("Codec"),
        "width": video.get("Width"),
        "height": video.get("Height"),
        "fps": video.get("AverageFrameRate") or video.get("RealFrameRate"),
        "video_bitrate": video.get("BitRate") or source.get("Bitrate"),
        "audio_codec": audio.get("Codec") if audio else None,
        "audio_channels": audio.get("Channels") if audio else None,
        "wall_safe": not violations,
        "violations": violations,
    }


def _print_items(items: list[dict[str, Any]], wall_safe_only: bool) -> int:
    records = []
    for item in items:
        violations = wall_safe_violations(item)
        if wall_safe_only and violations:
            continue
        records.append(_item_summary(item, violations))
    print(json.dumps(records, indent=2, sort_keys=True))
    if wall_safe_only and not records:
        print(
            json.dumps(
                {"status": "BLOCK", "reason": "no_wall_safe_items"},
                sort_keys=True,
            )
        )
        return 2
    return 0


def _runner_args(args: argparse.Namespace, stream_url: str) -> list[str]:
    runner = ROOT / "scripts" / "run-linux-disconnect-benchmark.py"
    command = [
        sys.executable,
        os.fspath(runner),
        "--input", stream_url,
        "--output", os.fspath(args.output),
        "--cells", str(args.cells),
        "--duration-s", str(args.duration_s),
        "--poll-s", str(args.poll_s),
        "--mpv", args.mpv,
        "--hwdec", args.hwdec,
        "--nvidia-smi", args.nvidia_smi,
    ]
    if args.drm_root is not None:
        command.extend(["--drm-root", os.fspath(args.drm_root)])
    if args.keep_awake:
        command.append("--keep-awake")
    if args.require_disconnect:
        command.append("--require-disconnect")
    return command


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cells < 1 or args.cells > 32:
        raise SystemExit("--cells must be between 1 and 32")
    if args.duration_s <= 0 or args.poll_s <= 0:
        raise SystemExit("--duration-s and --poll-s must be positive")

    session: requests.Session | None = None
    try:
        server_url, username, password, configured_library = _load_login(args.config)
        library = (args.library or configured_library).strip()
        if not library:
            raise EmbyBenchmarkError(
                "no library selected; pass --library or set Settings.last_libraries"
            )
        active_session, user_id, token = _authenticate(server_url, username, password)
        session = active_session
        items = _library_items(active_session, server_url, user_id, library)

        if args.list_items or args.list_wall_safe:
            return _print_items(items, args.list_wall_safe)
        if not args.item_id:
            raise EmbyBenchmarkError("--item-id is required for a benchmark run")
        if args.output is None:
            raise EmbyBenchmarkError("--output is required for a benchmark run")

        item = next((item for item in items if str(item.get("Id")) == args.item_id), None)
        if item is None:
            raise EmbyBenchmarkError(
                f"item {args.item_id!r} was not found in library {library!r}"
            )
        violations = wall_safe_violations(item)
        item_summary = _item_summary(item, violations)
        if violations:
            print(
                json.dumps(
                    {
                        "status": "NOTICE",
                        "reason": "item_not_wall_safe_benchmark_continues",
                        "item": item_summary,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )

        stream_url = build_stream_url(
            base=server_url,
            item_id=args.item_id,
            api_key=token,
            session_id=uuid.uuid4().hex,
            transcode=False,
            static=True,
        )
        print(
            json.dumps(
                {
                    "status": "STARTING",
                    "source": "authenticated_emby_direct_stream",
                    "library": library,
                    "item": item_summary,
                },
                sort_keys=True,
            )
        )
        return subprocess.run(_runner_args(args, stream_url), check=False).returncode
    except EmbyBenchmarkError as exc:
        print(json.dumps({"status": "BLOCK", "reason": str(exc)}, sort_keys=True))
        return 2
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
