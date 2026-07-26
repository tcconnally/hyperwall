"""Regression contract for ephemeral Hyperwall endpoint overrides.

A macOS playback A/B must be able to use Greg's LAN Emby endpoint without
rewriting a configured public endpoint or exposing credentials in commands.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hyperwall.config import effective_server_url  # noqa: E402


def test_endpoint_override_wins_without_mutating_default():
    configured = "https://mb.perseus.observer"
    override = "http://10.168.168.29:8096"
    assert effective_server_url(configured, override) == override
    assert configured == "https://mb.perseus.observer"


def test_blank_override_keeps_configured_endpoint():
    assert effective_server_url("http://emby:8096", "   ") == "http://emby:8096"
    assert effective_server_url("http://emby:8096", None) == "http://emby:8096"


def run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS  {test.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {test.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {len(tests)} tests.")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run_all() else 0)
