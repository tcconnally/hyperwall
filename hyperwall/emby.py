"""
Hyperwall — Emby REST API client.

Handles authentication, content loading, tag/favorite mutations,
and cleanup of tagged items.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

import requests
import urllib3
from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from . import VERSION_SHORT
from .backends import (
    EMBY,
    BackendSpec,
    auth_request_headers,
    token_headers,
)
from .renditions import NORMALIZATION_RECEIPT, explicit_receipt_range, prefer_rendition, valid_receipt_response

logger = logging.getLogger("HyperWall")

# Auto-transcode heuristic: sources beyond the fps/bitrate direct-play
# budget get server-side downscale. Override with HYPERWALL_AUTO_TRANSCODE=0.
_AUTO_TRANSCODE = os.environ.get("HYPERWALL_AUTO_TRANSCODE", "1") == "1"


def needs_transcode(item: dict[str, Any]) -> bool:
    """Heuristic: return True if the source exceeds the fps/bitrate
    direct-play budget (resolution is not a gate — see urls.needs_transcode).

    Thin wrapper over the pure helper in urls.py, binding the resolved
    HYPERWALL_AUTO_TRANSCODE flag and the direct-play budget constants.
    Kept here for backward-compatible imports.
    """
    from .constants import MAX_DIRECT_BITRATE_MBPS, MAX_DIRECT_FPS
    from .urls import needs_transcode as _needs_transcode

    return _needs_transcode(
        item,
        auto_transcode=_AUTO_TRANSCODE,
        max_fps=MAX_DIRECT_FPS,
        max_bitrate_mbps=MAX_DIRECT_BITRATE_MBPS,
    )


class EmbyClient:
    """Authenticated Emby REST API session."""

    def __init__(
        self,
        server_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        backend: "BackendSpec | None" = None,
    ):
        self.server_url = server_url.rstrip("/")
        self.username = username
        self._password = password
        self.verify_ssl = verify_ssl
        self.access_token: str | None = None
        self.user_id: str | None = None
        self._auth_lock = threading.Lock()
        self._device_id = f"hyperwall-{os.urandom(4).hex()}"
        self.backend = backend or EMBY

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": f"HyperWall/{VERSION_SHORT}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        })

    # ── connection lifecycle ──────────────────────────────────────────────

    def test_connection(self) -> bool:
        """Verify the Emby server is reachable."""
        try:
            r = self._session.get(
                f"{self.server_url}/System/Info/Public",
                timeout=5,
                verify=self.verify_ssl,
            )
            return r.status_code == 200
        except requests.RequestException:
            return False

    def authenticate(self) -> bool:
        """Authenticate and store the access token."""
        with self._auth_lock:
            try:
                r = self._session.post(
                    f"{self.server_url}/Users/AuthenticateByName",
                    headers=auth_request_headers(
                        self.backend, self._device_id, VERSION_SHORT,
                    ),
                    json={"Username": self.username, "Pw": self._password},
                    timeout=10,
                    verify=self.verify_ssl,
                )
                r.raise_for_status()
                d = r.json()
                self.access_token = d.get("AccessToken")
                self.user_id = d.get("User", {}).get("Id")
                logger.info("Authenticated. User ID: %s", self.user_id)
                return bool(self.access_token and self.user_id)
            except requests.RequestException as e:
                logger.error("Authentication error: %s", e)
                return False

    def close(self) -> None:
        self._session.close()

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return token_headers(self.backend, self.access_token)

    def get(self, path: str, **kw: Any) -> requests.Response:
        return self._session.get(
            f"{self.server_url}{path}",
            headers=self._headers(),
            verify=self.verify_ssl,
            **kw,
        )

    def post(self, path: str, **kw: Any) -> requests.Response:
        return self._session.post(
            f"{self.server_url}{path}",
            headers=self._headers(),
            verify=self.verify_ssl,
            **kw,
        )

    def delete(self, path: str, **kw: Any) -> requests.Response:
        return self._session.delete(
            f"{self.server_url}{path}",
            headers=self._headers(),
            verify=self.verify_ssl,
            **kw,
        )

    # ── content queries ───────────────────────────────────────────────────

    def fetch_libraries(self) -> list[str]:
        """Return sorted list of library names."""
        try:
            r = self.get(f"/Users/{self.user_id}/Views", timeout=10)
            items = r.json().get("Items", [])
            return sorted(v["Name"] for v in items)
        except Exception as e:
            logger.error("Failed to fetch libraries: %s", e)
            return []

    def fetch_items(
        self,
        library_names: list[str],
        progress_callback: callable | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all items from the given libraries."""
        all_items: list[dict[str, Any]] = []
        try:
            views = self.get(
                f"/Users/{self.user_id}/Views", timeout=10
            ).json().get("Items", [])
            view_map = {v["Name"]: v["Id"] for v in views}

            for lib in library_names:
                lid = view_map.get(lib)
                if not lid:
                    logger.warning("Library '%s' not found.", lib)
                    continue
                if progress_callback:
                    progress_callback(f"Loading '{lib}'...")
                try:
                    # Paginate: a single fixed Limit silently truncated
                    # libraries beyond it while logging a success-looking
                    # count (2026-07-13 audit).
                    items: list[dict[str, Any]] = []
                    page = 5_000
                    while True:
                        body = self.get(
                            f"/Users/{self.user_id}/Items",
                            params={
                                "ParentId": lid,
                                "Recursive": "true",
                                "IncludeItemTypes": "Video,MusicVideo,Movie,Episode",
                                "Fields": "MediaSources,MediaStreams,UserData,Tags",
                                "StartIndex": str(len(items)),
                                "Limit": str(page),
                            },
                            timeout=30,
                        ).json()
                        batch = body.get("Items", [])
                        items.extend(batch)
                        total = body.get("TotalRecordCount", len(items))
                        if not batch or len(items) >= total:
                            break
                    logger.info("Library '%s': %d items", lib, len(items))
                    all_items.extend(items)
                except requests.RequestException as e:
                    logger.error(
                        "Library '%s' failed (%s) — keeping %d items from prior libs.",
                        lib, e, len(all_items),
                    )
        except Exception as e:
            logger.error("Content loader error: %s", e)

        rendition_root = os.environ.get("HYPERWALL_RENDITION_ROOT", "").strip()
        prepared_only = os.environ.get("HYPERWALL_PREPARED_ONLY", "0") == "1"
        if rendition_root:
            all_items = self._prefer_renditions(all_items, rendition_root, progress_callback)
            logger.info(
                "Renditions selected: %d/%d; normalization receipts verified: %d.",
                sum(bool(item.get("_hyperwall_media_source_id")) for item in all_items),
                len(all_items),
                sum(item.get("_hyperwall_prepared") is True for item in all_items),
            )
        if prepared_only:
            total = len(all_items)
            all_items = (
                [item for item in all_items if item.get("_hyperwall_prepared") is True]
                if rendition_root else []
            )
            summary = f"Prepared-only playback: {len(all_items)}/{total} items ready"
            logger.info(summary)
            if progress_callback:
                progress_callback(summary)
            if not rendition_root:
                logger.error("Prepared-only playback requires HYPERWALL_RENDITION_ROOT; no items admitted.")
            elif not all_items:
                logger.error("No normalization receipts verified; prepared-only playback has no ready items.")
        return all_items

    def _prefer_renditions(
        self,
        items: list[dict[str, Any]],
        root: str,
        progress_callback: callable | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve Folder Sync sources on the content-loading worker.

        Emby 4.9 exposes synced sources through PlaybackInfo but omits them
        from both the bulk library response and item details. Do not make
        these network calls on the GUI thread or drop originals on failure.
        """
        selected: list[dict[str, Any]] = []
        consecutive_failures = 0
        verify_audio = (
            os.environ.get("HYPERWALL_AUDIO_MODE", "lazy").strip().lower() == "prepared"
            or os.environ.get("HYPERWALL_PREPARED_ONLY", "0") == "1"
        )
        for index, item in enumerate(items):
            enriched = prefer_rendition(item, root)
            if enriched is item and item.get("Id") and consecutive_failures < 3:
                try:
                    response = self.get(
                        f"/Items/{item['Id']}/PlaybackInfo",
                        params={"UserId": self.user_id},
                        timeout=5,
                    )
                    response.raise_for_status()
                    body = response.json()
                    if not isinstance(body, dict) or not isinstance(body.get("MediaSources"), list):
                        raise ValueError("invalid playback source response")
                    candidate = dict(item)
                    candidate["MediaSources"] = body["MediaSources"]
                    prepared = prefer_rendition(candidate, root)
                    if prepared.get("_hyperwall_media_source_id"):
                        enriched = prepared
                    consecutive_failures = 0
                except (requests.RequestException, ValueError, TypeError) as exc:
                    consecutive_failures += 1
                    logger.warning("Prepared source lookup failed (%s); original retained.", type(exc).__name__)
                    if consecutive_failures == 3:
                        logger.warning("Prepared source lookup paused after three failures; remaining originals retained.")
            if verify_audio and enriched.get("_hyperwall_media_source_id"):
                enriched["_hyperwall_prepared"] = self._has_normalization_receipt(enriched)
            selected.append(enriched)
            if progress_callback and (index % 25 == 0 or index + 1 == len(items)):
                progress_callback(f"Checking prepared sources: {index + 1}/{len(items)}")
        return selected

    def _has_normalization_receipt(self, item: dict[str, Any]) -> bool:
        """Read a bounded authenticated suffix; never follow redirects/download video."""
        response = None
        try:
            def read_range(value: str):
                return self._session.get(
                    f"{self.server_url}/Videos/{item['Id']}/stream",
                    params={"Static": "true", "MediaSourceId": item["_hyperwall_media_source_id"]},
                    headers={**self._headers(), "Range": value, "Accept-Encoding": "identity"},
                    verify=self.verify_ssl,
                    timeout=(3, 3),
                    allow_redirects=False,
                    stream=True,
                )
            response = read_range("bytes=-64")
            content_range = response.headers.get("Content-Range", "")
            explicit_range = explicit_receipt_range(response.status_code, content_range)
            if explicit_range is not None and response.headers.get("Content-Length") in (None, "65"):
                # Do not read the incorrect prefix response. Emby's returned
                # total lets us ask for only the tail, without trusting stale
                # library metadata or issuing a full-file request.
                response.close()
                response = read_range(explicit_range)
                content_range = response.headers.get("Content-Range", "")
            # Validate the range before reading any body, including a server
            # that ignores Range and returns the whole video with status 200.
            if not valid_receipt_response(response.status_code, content_range, NORMALIZATION_RECEIPT):
                return False
            length = response.headers.get("Content-Length")
            if length is not None and length != "64":
                return False
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                return False
            body = response.raw.read(65, decode_content=False)
            return valid_receipt_response(response.status_code, content_range, body)
        except (requests.RequestException, urllib3.exceptions.HTTPError, OSError, ValueError, TypeError, KeyError):
            # Exception strings may contain authenticated URLs; do not log them.
            return False
        finally:
            if response is not None:
                try:
                    response.close()
                except (requests.RequestException, urllib3.exceptions.HTTPError, OSError):
                    pass


# ── Background Workers ────────────────────────────────────────────────────────


class ContentLoader(QThread):
    """Loads library items in a background thread."""

    finished = pyqtSignal(list)
    progress = pyqtSignal(str)

    def __init__(self, client: EmbyClient, library_names: list[str]):
        super().__init__()
        self.client = client
        self.library_names = library_names

    def run(self) -> None:
        items = self.client.fetch_items(
            self.library_names,
            progress_callback=self.progress.emit,
        )
        self.finished.emit(items)


class CleanupWorker(QObject):
    """Deletes items tagged 'ToDelete' in a background thread."""

    finished = pyqtSignal(int, int)
    progress = pyqtSignal(str)

    def __init__(self, client: EmbyClient):
        super().__init__()
        self.client = client
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @pyqtSlot()
    def run(self) -> None:
        logger.info("Maintenance: Starting cleanup...")
        try:
            r = self.client.get(
                f"/Users/{self.client.user_id}/Items",
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Video,MusicVideo,Movie,Episode",
                    "Tags": "ToDelete",
                    "Limit": "500",
                },
                timeout=10,
            )
            items = r.json().get("Items", [])
            if not items:
                self.finished.emit(0, 0)
                return

            ok, fail = 0, 0
            for item in items:
                if self._cancelled:
                    break
                name = item.get("Name", "Unknown")
                self.progress.emit(name)
                try:
                    r = self.client.delete(f"/Items/{item['Id']}", timeout=7)
                    if r.status_code >= 300:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    logger.info("Maintenance: Deleted '%s'", name)
                    ok += 1
                except Exception as e:
                    logger.error("Maintenance: Failed to delete '%s': %s", name, e)
                    fail += 1

            self.finished.emit(ok, fail)
        except Exception as e:
            logger.error("Maintenance error: %s", e)
            self.finished.emit(0, -1)
