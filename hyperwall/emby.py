"""
Hyperwall v9 — Emby REST API client.

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

logger = logging.getLogger("HyperWall")

# Auto-transcode heuristic: sources >1080p get server-side downscale.
# Override with HYPERWALL_AUTO_TRANSCODE=0.
_AUTO_TRANSCODE = os.environ.get("HYPERWALL_AUTO_TRANSCODE", "1") == "1"


def needs_transcode(item: dict[str, Any]) -> bool:
    """Heuristic: return True if the source exceeds 1080p."""
    if not _AUTO_TRANSCODE:
        return False
    src = (item.get("MediaSources") or [{}])[0]
    streams = src.get("MediaStreams") or item.get("MediaStreams") or []
    v = next((s for s in streams if s.get("Type") == "Video"), {}) or {}
    w = v.get("Width") or 0
    h = v.get("Height") or 0
    return w > 1920 or h > 1080


class EmbyClient:
    """Authenticated Emby REST API session."""

    def __init__(
        self,
        server_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
    ):
        self.server_url = server_url.rstrip("/")
        self.username = username
        self._password = password
        self.verify_ssl = verify_ssl
        self.access_token: str | None = None
        self.user_id: str | None = None
        self._auth_lock = threading.Lock()
        self._device_id = f"hyperwall-{os.urandom(4).hex()}"

        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "HyperWall/9.0",
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
                    headers={
                        "Content-Type": "application/json",
                        "X-Emby-Authorization": (
                            f'MediaBrowser Client="HyperWall", Device="PC", '
                            f'DeviceId="{self._device_id}", Version="9.0"'
                        ),
                    },
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
        return {"X-Emby-Token": self.access_token or ""}

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
                    items = self.get(
                        f"/Users/{self.user_id}/Items",
                        params={
                            "ParentId": lid,
                            "Recursive": "true",
                            "IncludeItemTypes": "Video,MusicVideo,Movie,Episode",
                            "Fields": "MediaSources,MediaStreams,UserData,Tags",
                            "Limit": "10000",
                        },
                        timeout=30,
                    ).json().get("Items", [])
                    logger.info("Library '%s': %d items", lib, len(items))
                    all_items.extend(items)
                except requests.RequestException as e:
                    logger.error(
                        "Library '%s' failed (%s) — keeping %d items from prior libs.",
                        lib, e, len(all_items),
                    )
        except Exception as e:
            logger.error("Content loader error: %s", e)

        return all_items


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
                    self.client.delete(f"/Items/{item['Id']}", timeout=7)
                    logger.info("Maintenance: Deleted '%s'", name)
                    ok += 1
                except Exception as e:
                    logger.error("Maintenance: Failed to delete '%s': %s", name, e)
                    fail += 1

            self.finished.emit(ok, fail)
        except Exception as e:
            logger.error("Maintenance error: %s", e)
            self.finished.emit(0, -1)
