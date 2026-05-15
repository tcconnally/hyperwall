import os
import logging
import threading
import requests
import urllib3
from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("HyperWall")

# ── Hybrid URL routing ────────────────────────────────────────────────────────
# Default to normalizing >1080p sources before they hit the wall. Direct 4K is
# fine in isolation, but 8 simultaneous cells create visible frame pacing pain.
_AUTO_TRANSCODE = os.environ.get("HYPERWALL_AUTO_TRANSCODE", "1") == "1"

def needs_transcode(item: dict) -> bool:
    if not _AUTO_TRANSCODE:
        return False
    src = (item.get("MediaSources") or [{}])[0]
    streams = src.get("MediaStreams") or item.get("MediaStreams") or []
    v = next((s for s in streams if s.get("Type") == "Video"), {}) or {}
    w = v.get("Width") or 0
    h = v.get("Height") or 0
    return w > 1920 or h > 1080

# ── Emby API Session ──────────────────────────────────────────────────────────
class EmbyAPISession:
    def __init__(self, server_url: str, username: str, password: str):
        self.server_url = server_url.rstrip("/")
        self.username   = username
        self._password  = password
        self.access_token: str | None = None
        self.user_id: str | None      = None
        self._auth_lock = threading.Lock()
        self._device_id = f"hyperwall-{os.urandom(4).hex()}"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":      "HyperWall/8.0",
            "Accept":          "application/json",
            "Accept-Encoding": "gzip, deflate",
        })

    def test_connection(self) -> bool:
        try:
            r = self.session.get(f"{self.server_url}/System/Info/Public",
                                 timeout=5, verify=False)
            return r.status_code == 200
        except requests.exceptions.RequestException as e:
            logger.error("Connection test failed: %s", e)
            return False

    def authenticate(self) -> bool:
        with self._auth_lock:
            try:
                r = self.session.post(
                    f"{self.server_url}/Users/AuthenticateByName",
                    headers={
                        "Content-Type": "application/json",
                        "X-Emby-Authorization": (
                            f'MediaBrowser Client="HyperWall", Device="PC", '
                            f'DeviceId="{self._device_id}", Version="8.0"'
                        ),
                    },
                    json={"Username": self.username, "Pw": self._password},
                    timeout=10, verify=False,
                )
                r.raise_for_status()
                d = r.json()
                self.access_token = d.get("AccessToken")
                self.user_id      = d.get("User", {}).get("Id")
                logger.info("Authenticated. User ID: %s", self.user_id)
                return bool(self.access_token and self.user_id)
            except requests.exceptions.RequestException as e:
                logger.error("Authentication error: %s", e)
                return False

    def _h(self) -> dict: return {"X-Emby-Token": self.access_token}

    def get(self, path: str, **kw):
        return self.session.get(f"{self.server_url}{path}", headers=self._h(), verify=False, **kw)

    def post(self, path: str, **kw):
        return self.session.post(f"{self.server_url}{path}", headers=self._h(), verify=False, **kw)

    def delete(self, path: str, **kw):
        return self.session.delete(f"{self.server_url}{path}", headers=self._h(), verify=False, **kw)

    def close(self): self.session.close()

# ── Background Workers ────────────────────────────────────────────────────────
class CleanupWorker(QObject):
    finished = pyqtSignal(int, int)
    progress = pyqtSignal(str)

    def __init__(self, api: EmbyAPISession):
        super().__init__()
        self.api = api
        self._cancelled = False

    @pyqtSlot()
    def run(self):
        logger.info("Maintenance: Starting cleanup...")
        path_items = ""
        try:
            path_items = f"/Users/{self.api.user_id}/Items"
            r = self.api.get(
                path_items,
                params={
                    "Recursive": "true",
                    "IncludeItemTypes": "Video,MusicVideo,Movie,Episode",
                    "Tags": "ToDelete",
                    "Limit": "500",
                }, timeout=10,
            )
            items = r.json().get("Items", [])
            if not items:
                self.finished.emit(0, 0); return
            ok, fail = 0, 0
            for item in items:
                if self._cancelled:
                    break
                name = item.get("Name", "Unknown")
                self.progress.emit(name)
                path_delete_item = ""
                try:
                    path_delete_item = f"/Items/{item['Id']}"
                    self.api.delete(path_delete_item, timeout=7)
                    logger.info("Maintenance: Deleted '%s'", name)
                    ok += 1
                except requests.exceptions.RequestException as e:
                    logger.error("Maintenance: Failed to delete '%s' via %s: %s", name, path_delete_item, e)
                    fail += 1
            self.finished.emit(ok, fail)
        except requests.exceptions.RequestException as e:
            logger.error("Maintenance failed to fetch items from %s: %s", path_items, e)
            self.finished.emit(0, -1)
        except Exception as e:
            logger.error("Maintenance crash: %s", e) # Catch other potential non-request errors
            self.finished.emit(0, -1)

class ContentLoaderThread(QThread):
    finished = pyqtSignal(list)
    progress = pyqtSignal(str)

    def __init__(self, api: EmbyAPISession, library_names: list[str]):
        super().__init__()
        self.api = api
        self.library_names = library_names

    def run(self):
        all_items: list[dict] = []
        path_views = ""
        path_library_items = ""
        try:
            path_views = f"/Users/{self.api.user_id}/Views"
            views = self.api.get(path_views, timeout=10).json().get("Items", [])
            view_map = {v["Name"]: v["Id"] for v in views}
            for lib in self.library_names:
                lid = view_map.get(lib)
                if not lid:
                    logger.warning("Library '%s' not found.", lib); continue
                self.progress.emit(f"Loading '{lib}'…")
                path_library_items = f"/Users/{self.api.user_id}/Items"
                items = self.api.get(
                    path_library_items,
                    params={
                        "ParentId": lid, "Recursive": "true",
                        "IncludeItemTypes": "Video,MusicVideo,Movie,Episode",
                        "Fields": "MediaSources,MediaStreams,UserData,Tags",
                        "Limit": "10000",
                    }, timeout=30,
                ).json().get("Items", [])
                logger.info("Library '%s': %d items", lib, len(items))
                all_items.extend(items)
        except requests.exceptions.RequestException as e:
            logger.error("Content loader error while fetching %s or library items: %s", path_views, e)
            all_items.clear() # Ensure no partial data is returned on error
        except Exception as e:
            logger.error("Content loader crash: %s", e)
            all_items.clear()
        self.finished.emit(all_items)
