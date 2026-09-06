"""macOS-only runtime policy for Hyperwall."""
from __future__ import annotations

import os
import sys


_MACOS_DECODER_PROFILES = {
    "safe": "no",
    "software": "no",
    "hardware-copy": "videotoolbox-copy",
    "hardware": "videotoolbox",
}
_SUPPORTED_OVERRIDES = {
    "no",
    "videotoolbox",
    "videotoolbox-copy",
    "auto",
    "auto-safe",
}


def require_macos(platform: str | None = None) -> None:
    """Fail closed when the native application is not running on macOS."""
    target = sys.platform if platform is None else str(platform)
    if target != "darwin":
        raise RuntimeError(f"Hyperwall is macOS-only; found platform {target!r}")


def decoder_for_profile(
    profile: str | None = None,
    *,
    override: str | None = None,
) -> str:
    """Resolve one explicit macOS decoder choice.

    The default ``safe`` profile is software decode because the measured M5
    VideoToolbox soak was not clean. Hardware profiles remain explicit so a
    target-host pilot can select them without changing code or inferring a
    decoder from RAM size.
    """
    selected_override = (
        os.environ.get("HYPERWALL_HWDEC") if override is None else override
    )
    if selected_override is not None and str(selected_override).strip():
        value = str(selected_override).strip().lower()
        if value not in _SUPPORTED_OVERRIDES:
            raise ValueError(
                "Unsupported macOS decoder override: "
                f"{selected_override!r}; expected one of "
                f"{sorted(_SUPPORTED_OVERRIDES)}"
            )
        return value

    selected_profile = (
        os.environ.get("HYPERWALL_M5_DECODER_PROFILE", "safe")
        if profile is None else profile
    )
    key = str(selected_profile or "safe").strip().lower()
    try:
        return _MACOS_DECODER_PROFILES[key]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported macOS decoder profile: {selected_profile!r}; "
            f"expected one of {sorted(_MACOS_DECODER_PROFILES)}"
        ) from exc


__all__ = ["decoder_for_profile", "require_macos"]
