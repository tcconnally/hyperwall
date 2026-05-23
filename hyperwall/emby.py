import os
import logging
import threading
import requests
import urllib3
from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

logger = logging.getLogger("HyperWall")

# ── Hybrid URL routing (matches INSTRUCTIONS_v8.md) ─────────────────────────
# Default: Always REMUX path.
# Escalation: On retry #2 in VideoCell._on_error, _force_transcode flips the
# Emby request to VideoCodec=h264 (transcode).  classify_item() provides the
# pre-flight signal for when direct stream is unlikely to succeed.
# Default to normalizing >1080p sources before they hit the wall. Direct 4K is
# fine in isolation, but 8 simultaneous cells create visible frame pacing pain.
_AUTO_TRANSCODE = os.environ.get("HYPERWALL_AUTO_TRANSCODE", "1") == "1"

# ── Content classifier (v8.2 production hardening) ──────────────────────────
# Replaces the single resolution-gate needs_transcode() with a multi-factor
# decision tree.  Returns classification + evidence for logging.
#
# Tiers (in order of severity):
#   "immediate"  — force transcode on first attempt (known-hostile codec/HDR)
#   "auto"       — auto-transcode (resolution, bitrate, subtitle burn-in)
#   "direct"     — REMUX path is safe

def classify_item(item: dict) -> str:
    """Return 'immediate', 'auto', or 'direct' for stream routing.

    Factors considered: codec, colour depth, HDR, subtitle burn-in,
    resolution, bitrate, and reference-frame pressure.
    """
    if not _AUTO_TRANSCODE:
        return "direct"

    src = (item.get("MediaSources") or [{}])[0]
    streams = src.get("MediaStreams") or item.get("MediaStreams") or []
    v = next((s for s in streams if s.get("Type") == "Video"), {}) or {}
    subs = [s for s in streams if s.get("Type") == "Subtitle"]

    codec = (v.get("Codec") or "").lower()
    width = v.get("Width") or 0
    height = v.get("Height") or 0
    bitrate = src.get("Bitrate") or 0
    hdr = v.get("VideoRange") or "SDR"
    ref_frames = v.get("RefFrames") or 0
    level = v.get("Level") or 0

    # ── Tier 1: codec gates (HW-decode-hostile) ──────────────────────────
    HWDEC_HOSTILE = {"hevc", "av1", "vp9", "mpeg4", "wmv3", "vc1"}
    if codec in HWDEC_HOSTILE:
        if codec == "hevc" and (v.get("Profile") or "").endswith("10"):
            logger.info("classify: HEVC 10-bit → immediate transcode")
            return "immediate"
        if codec == "av1" and ref_frames > 8:
            logger.info("classify: AV1 high-ref → immediate transcode")
            return "immediate"
        if codec == "vp9" and (width > 1920 or height > 1080):
            logger.info("classify: VP9 >1080p → immediate transcode")
            return "immediate"

    # ── Tier 2: HDR gate ────────────────────────────────────────────────
    if hdr not in ("SDR", ""):
        logger.info("classify: %s → auto transcode (HDR tone-map)", hdr)
        return "auto"

    # ── Tier 3: subtitle burn-in gate ────────────────────────────────────
    BURNIN_CODECS = {"pgs", "dvbsub", "dvdsub", "xsub"}
    if any(s.get("Codec", "").lower() in BURNIN_CODECS
           and not s.get("IsExternal", False)
           for s in subs if s.get("DeliveryMethod") != "External"):
        logger.info("classify: burn-in subs → auto transcode")
        return "auto"

    # ── Tier 4: resolution gate ─────────────────────────────────────────
    if width > 1920 or height > 1080:
        logger.info("classify: %dx%d → auto transcode", width, height)
        return "auto"

    # ── Tier 5: bitrate pressure gate ───────────────────────────────────
    if bitrate > 25_000_000:
        logger.info("classify: %d Mbps → auto transcode", bitrate // 1_000_000)
        return "auto"

    # ── Tier 6: H.264 level gate ────────────────────────────────────────
    if codec == "h264" and level > 50:
        logger.info("classify: H.264 level %.1f → auto transcode", level)
        return "auto"

    return "direct"


def needs_transcode(item: dict) -> bool:
    """Legacy boolean wrapper — prefer classify_item() for new code."""
    return classify_item(item) != "direct"

# ── Emby API Session ──────────────────────────────────────────────────────────
class EmbyAPISession:
    def __init__(self, server_url: str, username: str, password: str):
        self.verify_ssl = True
        self.server_url = server_url.rstrip("/")
        self.username   = username
        self._password  = password
        self.access_token: str | None = None
        self.user_id: str | None      = None
        self._auth_lock = threading.Lock()
        self._device_id = f"hyperwall-{os.urandom(4).hex()}"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":      "HyperWall/8.2",
            "Accept":          "application/json",
            "Accept-Encoding": "gzip, deflate",
        })

    def test_connection(self) -> bool:
        try:
            r = self.session.get(f"{self.server_url}/System/Info/Public",
                                 timeout=5, verify=self.verify_ssl)
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
                            f'DeviceId="{self._device_id}", Version="8.2"'
                        ),
                    },
                    json={"Username": self.username, "Pw": self._password},
                    timeout=10, verify=self.verify_ssl,
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
        return self.session.get(f"{self.server_url}{path}", headers=self._h(), verify=self.verify_ssl, **kw)

    def post(self, path: str, **kw):
        return self.session.post(f"{self.server_url}{path}", headers=self._h(), verify=self.verify_ssl, **kw)

    def delete(self, path: str, **kw):
        return self.session.delete(f"{self.server_url}{path}", headers=self._h(), verify=self.verify_ssl, **kw)

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
