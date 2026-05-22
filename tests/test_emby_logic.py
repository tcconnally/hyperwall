"""
Unit tests for needs_transcode() and tag parsing logic.

Pure-Python, no Emby server required.  Run with:
    pytest tests/test_emby_logic.py -v
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal stubs so emby.py can import without PyQt6 / requests installed
# in lightweight CI environments.
# ---------------------------------------------------------------------------
def _stub_module(name: str):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod

for _pkg in ("PyQt6", "PyQt6.QtCore", "PyQt6.QtWidgets", "PyQt6.QtGui"):
    if _pkg not in sys.modules:
        _stub_module(_pkg)

# Stub out the Qt symbols emby.py uses
_qcore = sys.modules["PyQt6.QtCore"]
for _sym in ("QObject", "QThread", "pyqtSignal", "pyqtSlot"):
    setattr(_qcore, _sym, MagicMock())

# Stub requests at the module level so EmbyAPISession doesn't break
if "requests" not in sys.modules:
    _req = _stub_module("requests")
    _req.Session = MagicMock()
    _req_exc = _stub_module("requests.exceptions")
    _req_exc.RequestException = Exception

# Stub urllib3
if "urllib3" not in sys.modules:
    _stub_module("urllib3")

# Make sure the hyperwall package root is on sys.path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Import emby directly by file path to avoid the hyperwall/__init__.py →
# main.py → PyQt6 cascade that fires when importing via the package.
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location("hyperwall.emby", ROOT / "hyperwall" / "emby.py")
_emby_mod = _ilu.module_from_spec(_spec)
sys.modules["hyperwall.emby"] = _emby_mod
_spec.loader.exec_module(_emby_mod)

from hyperwall.emby import needs_transcode  # noqa: E402  (after stubs)


# ===========================================================================
# needs_transcode() — resolution heuristic
# ===========================================================================

def _item(width: int, height: int) -> dict:
    """Build a minimal Emby item dict with the given video stream dimensions."""
    return {
        "MediaSources": [{
            "MediaStreams": [
                {"Type": "Video", "Width": width, "Height": height},
                {"Type": "Audio", "Codec": "aac"},
            ]
        }]
    }


class TestNeedsTranscode:
    def test_1080p_is_direct(self):
        assert needs_transcode(_item(1920, 1080)) is False

    def test_1080p_exact_boundary(self):
        # Exactly 1920×1080 — must not transcode
        assert needs_transcode(_item(1920, 1080)) is False

    def test_4k_uhd_triggers_transcode(self):
        assert needs_transcode(_item(3840, 2160)) is True

    def test_width_only_over_limit(self):
        # Width > 1920 but height ≤ 1080 — still triggers
        assert needs_transcode(_item(2560, 1080)) is True

    def test_height_only_over_limit(self):
        # 1440p vertical (e.g. rotated portrait 4K clip)
        assert needs_transcode(_item(1920, 1440)) is True

    def test_720p_is_direct(self):
        assert needs_transcode(_item(1280, 720)) is False

    def test_sd_is_direct(self):
        assert needs_transcode(_item(720, 480)) is False

    def test_missing_video_stream_is_direct(self):
        item = {"MediaSources": [{"MediaStreams": [{"Type": "Audio", "Codec": "aac"}]}]}
        assert needs_transcode(item) is False

    def test_empty_media_sources_is_direct(self):
        assert needs_transcode({"MediaSources": []}) is False

    def test_no_media_sources_key_is_direct(self):
        assert needs_transcode({}) is False

    def test_media_streams_at_top_level_fallback(self):
        # Some Emby responses put MediaStreams at the item root, not under MediaSources
        item = {
            "MediaStreams": [
                {"Type": "Video", "Width": 3840, "Height": 2160},
            ]
        }
        assert needs_transcode(item) is True

    def test_auto_transcode_env_disabled(self, monkeypatch):
        """HYPERWALL_AUTO_TRANSCODE=0 forces direct regardless of resolution."""
        monkeypatch.setenv("HYPERWALL_AUTO_TRANSCODE", "0")
        # Reload via direct file path (same pattern as module-level import above)
        # to avoid the hyperwall/__init__ → main.py → PyQt6 cascade.
        import importlib.util as _ilu
        from pathlib import Path
        _root = Path(__file__).resolve().parents[1]
        _spec = _ilu.spec_from_file_location(
            "hyperwall.emby_reload", _root / "hyperwall" / "emby.py"
        )
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        assert _mod.needs_transcode(_item(3840, 2160)) is False


# ===========================================================================
# Tag parsing — the inline normalization in WallController.update_tags()
# ===========================================================================
# The logic under test (extracted verbatim from controller.py):
#
#   raw  = item.get("Tags", [])
#   tags = ([t.get("Name", "") for t in raw]
#           if raw and isinstance(raw[0], dict) else list(raw))
#
# Emby returns Tags as either:
#   (a) list of dicts  — {"Name": "ToDelete", ...}  (rich API response)
#   (b) list of strs   — ["ToDelete", "Favorite"]   (simplified response)

def _parse_tags(item: dict) -> list[str]:
    """Mirror the tag-parsing one-liner from WallController.update_tags()."""
    raw = item.get("Tags", [])
    return (
        [t.get("Name", "") for t in raw]
        if raw and isinstance(raw[0], dict)
        else list(raw)
    )


class TestTagParsing:
    def test_dict_tags_extracts_name(self):
        item = {"Tags": [{"Name": "ToDelete"}, {"Name": "Favorite"}]}
        assert _parse_tags(item) == ["ToDelete", "Favorite"]

    def test_string_tags_pass_through(self):
        item = {"Tags": ["ToDelete", "Favorite"]}
        assert _parse_tags(item) == ["ToDelete", "Favorite"]

    def test_empty_tags_list(self):
        item = {"Tags": []}
        assert _parse_tags(item) == []

    def test_missing_tags_key(self):
        assert _parse_tags({}) == []

    def test_dict_tag_missing_name_falls_back_to_empty_string(self):
        item = {"Tags": [{"Id": "123"}, {"Name": "Keep"}]}
        result = _parse_tags(item)
        assert result == ["", "Keep"]

    def test_single_string_tag(self):
        item = {"Tags": ["ToDelete"]}
        assert _parse_tags(item) == ["ToDelete"]

    def test_single_dict_tag(self):
        item = {"Tags": [{"Name": "Seasonal"}]}
        assert _parse_tags(item) == ["Seasonal"]

    def test_mixed_list_starting_with_dict_uses_dict_path(self):
        # If first element is dict, all elements are treated as dicts
        item = {"Tags": [{"Name": "A"}, {"Name": "B"}]}
        assert _parse_tags(item) == ["A", "B"]

    def test_preserves_order(self):
        names = ["Zebra", "Alpha", "Mango"]
        item = {"Tags": [{"Name": n} for n in names]}
        assert _parse_tags(item) == names
