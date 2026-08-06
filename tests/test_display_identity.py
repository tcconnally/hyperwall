"""Headless tests for stable QScreen identity handling."""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from hyperwall.displays import (  # noqa: E402
    display_identity,
    restore_display_settings,
)


class _FakeScreen:
    def __init__(
        self,
        name: str,
        serial: str = "",
        manufacturer: str = "",
        model: str = "",
        connector: str = "",
    ) -> None:
        self._name = name
        self._serial = serial
        self._manufacturer = manufacturer
        self._model = model
        self._connector = connector

    def name(self) -> str:
        return self._name

    def serialNumber(self) -> str:
        return self._serial

    def manufacturer(self) -> str:
        return self._manufacturer

    def model(self) -> str:
        return self._model

    def connectorName(self) -> str:
        return self._connector


def test_serial_identity_survives_display_reordering_and_name_changes():
    original = _FakeScreen("LG ULTRAGEAR+", "SERIAL-A", "LG", "27GR")
    after_reorder = _FakeScreen("LG ULTRAGEAR+ (renamed)", "SERIAL-A", "LG", "27GR")
    other = _FakeScreen("LG ULTRAGEAR+", "SERIAL-B", "LG", "27GR")

    assert display_identity(original) == display_identity(after_reorder)
    assert display_identity(original) != display_identity(other)


def test_name_fallback_is_deterministic_without_serial_metadata():
    first = _FakeScreen("DisplayPort-1", manufacturer="Dell", model="U2720Q")
    second = _FakeScreen("DisplayPort-1", manufacturer="Dell", model="U2720Q")

    assert display_identity(first) == display_identity(second)
    assert display_identity(first).startswith("screen-v1:")


def test_serialless_identical_models_can_use_connector_identity():
    first = _FakeScreen("DisplayPort", manufacturer="Dell", model="U2720Q", connector="DP-1")
    second = _FakeScreen("DisplayPort", manufacturer="Dell", model="U2720Q", connector="DP-2")
    assert display_identity(first) != display_identity(second)


def test_identity_does_not_depend_on_display_list_position():
    left = _FakeScreen("Display A", "SERIAL-A", "Acme", "Panel")
    right = _FakeScreen("Display B", "SERIAL-B", "Acme", "Panel")

    assert [display_identity(left), display_identity(right)] != [
        display_identity(right),
        display_identity(left),
    ]


def test_identity_is_used_as_the_persisted_monitor_settings_key():
    with open(os.path.join(REPO_ROOT, "hyperwall", "wizard.py"), encoding="utf-8") as handle:
        source = handle.read()
    assert "display_identity(self._screen_map[l])" in source
    assert '"display_settings"' in source


def test_missing_or_reordered_monitor_uses_identity_and_safe_defaults():
    screen = _FakeScreen("Renamed", "SERIAL-A", "Acme", "Panel")
    persisted = {
        display_identity(_FakeScreen("Old name", "SERIAL-A", "Acme", "Panel")): {
            "selected": True,
            "role": "preview",
            "rotation": "270",
            "rows": 5,
            "cols": 2,
        }
    }
    assert restore_display_settings(screen, persisted) == {
        "selected": True,
        "role": "preview",
        "rotation": "270",
        "rows": 5,
        "cols": 2,
    }
    assert restore_display_settings(_FakeScreen("New", "SERIAL-B"), persisted) == {
        "selected": False,
        "role": "wall",
        "rotation": "auto",
        "rows": 2,
        "cols": 2,
    }


def test_connector_metadata_fallback_is_stable():
    first = _FakeScreen("Display", "", "Acme", "Panel")
    second = _FakeScreen("Renamed", "", "Acme", "Panel")
    first.connectorName = lambda: "HDMI-1"
    second.connectorName = lambda: "HDMI-1"
    assert display_identity(first) == display_identity(second)
    first.name = lambda: "Renamed"
    assert display_identity(first) == display_identity(second)


def run_all():
    tests = [
        test_serial_identity_survives_display_reordering_and_name_changes,
        test_name_fallback_is_deterministic_without_serial_metadata,
        test_serialless_identical_models_can_use_connector_identity,
        test_identity_does_not_depend_on_display_list_position,
        test_identity_is_used_as_the_persisted_monitor_settings_key,
        test_missing_or_reordered_monitor_uses_identity_and_safe_defaults,
        test_connector_metadata_fallback_is_stable,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures} passed, {failures} failed out of {len(tests)} tests.")
    return failures


if __name__ == "__main__":
    raise SystemExit(run_all())

__all__ = ["run_all"]
