"""
Unit tests for hyperwall.backends (Epic 5) — Emby/Jellyfin abstraction.

No PyQt / mpv / network. Run: python tests/test_backends.py

The key guarantees:
  - Emby behavior is byte-identical to what shipped pre-abstraction
    (X-Emby-Authorization request header, X-Emby-Token session header,
     MediaBrowser Client="HyperWall", static=true DIRECT url).
  - Jellyfin is a distinct spec, explicitly marked NOT verified_live.
  - Unknown backends fall back to Emby (safe default).
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from hyperwall.backends import (  # noqa: E402
    EMBY,
    JELLYFIN,
    auth_request_headers,
    auth_string,
    resolve_backend,
    token_headers,
)
from hyperwall.urls import build_stream_url  # noqa: E402


# ── resolution ────────────────────────────────────────────────────────────────

def test_resolve_default_is_emby():
    assert resolve_backend(None).name == "emby"
    assert resolve_backend("").name == "emby"


def test_resolve_is_case_insensitive():
    assert resolve_backend("EMBY").name == "emby"
    assert resolve_backend("Jellyfin").name == "jellyfin"


def test_unknown_falls_back_to_emby():
    assert resolve_backend("plex").name == "emby"


# ── Emby parity (must match pre-abstraction behavior byte-for-byte) ───────────

def test_emby_is_verified_live():
    assert EMBY.verified_live is True


def test_emby_auth_headers_unchanged():
    h = auth_request_headers(EMBY, device_id="dev123", version="10.0")
    assert h["Content-Type"] == "application/json"
    assert "X-Emby-Authorization" in h
    assert h["X-Emby-Authorization"] == (
        'MediaBrowser Client="HyperWall", Device="PC", '
        'DeviceId="dev123", Version="10.0"'
    )


def test_emby_token_header_unchanged():
    assert token_headers(EMBY, "TOK") == {"X-Emby-Token": "TOK"}
    assert token_headers(EMBY, None) == {"X-Emby-Token": ""}


def test_emby_direct_url_has_static_true():
    # LOAD-BEARING (Emby 4.9.5.0 500 workaround).
    url = build_stream_url(
        base="http://h", item_id="I", api_key="K", session_id="S",
        transcode=False, static=EMBY.requires_static_true,
    )
    assert "static=true" in url


# ── Jellyfin (distinct, NOT yet verified) ─────────────────────────────────────

def test_jellyfin_not_verified_live():
    # Guard: must stay False until proven against a real server.
    assert JELLYFIN.verified_live is False


def test_jellyfin_uses_authorization_header():
    h = auth_request_headers(JELLYFIN, device_id="d", version="10.0")
    assert "Authorization" in h
    assert "X-Emby-Authorization" not in h


def test_jellyfin_still_uses_static_true():
    # Jellyfin supports static=true; DIRECT path keeps it.
    url = build_stream_url(
        base="http://h", item_id="I", api_key="K", session_id="S",
        transcode=False, static=JELLYFIN.requires_static_true,
    )
    assert "static=true" in url


def test_auth_string_shape():
    s = auth_string(EMBY, "d", "10.0")
    assert s.startswith('MediaBrowser Client="HyperWall"')
    assert 'DeviceId="d"' in s
    assert 'Version="10.0"' in s


# ── config integration ────────────────────────────────────────────────────────

def test_config_default_backend_is_emby():
    from hyperwall.config import HyperwallConfig
    cfg = HyperwallConfig(server_url="http://h", username="u", password="p")
    assert cfg.backend == "emby"


def test_config_backend_round_trips():
    import tempfile
    from hyperwall.config import HyperwallConfig
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.ini")
        HyperwallConfig(
            server_url="http://h", username="u", password="p",
            backend="jellyfin",
        ).save(path)
        loaded = HyperwallConfig.load(path)
        assert loaded.backend == "jellyfin"
        assert resolve_backend(loaded.backend).name == "jellyfin"


def run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    sys.exit(run_all())
