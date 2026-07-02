"""
Hyperwall — media backend abstraction (Emby / Jellyfin). Pure, no network.

Emby and Jellyfin share almost their entire REST surface (Jellyfin is an Emby
fork), so the differences this app cares about are small and captured here as a
`BackendSpec`: client identity, the auth request/token header names, and
whether the DIRECT stream URL must carry `static=true`.

Keeping these as data (not scattered conditionals) gives one clean seam for
future divergence and makes the load-bearing choices explicit and testable.

⚠️ VALIDATION GATE: `verified_live` marks whether a backend has been proven
against a real server from this codebase. Emby is verified (the app has always
run on it). Jellyfin is NOT yet — its spec encodes the documented-compatible
values, but `resolve_backend` warns and callers must not treat it as blessed
until someone confirms auth + playback against a live Jellyfin instance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("HyperWall")


@dataclass(frozen=True)
class BackendSpec:
    """Per-backend knobs. Everything the Emby/Jellyfin split needs, as data."""

    name: str                    # canonical id: "emby" | "jellyfin"
    client_name: str             # MediaBrowser Client="..." identity
    auth_request_header: str     # header carrying the MediaBrowser auth string
    token_header: str            # header carrying the session token on requests
    requires_static_true: bool   # DIRECT url must append &static=true
    verified_live: bool          # proven against a real server from this repo


# Emby — the app's original, fully-exercised backend. Byte-for-byte the
# behavior that shipped before this abstraction existed.
EMBY = BackendSpec(
    name="emby",
    client_name="HyperWall",
    auth_request_header="X-Emby-Authorization",
    token_header="X-Emby-Token",
    requires_static_true=True,   # Emby 4.9.5.0 returns 500 on /stream without it
    verified_live=True,
)

# Jellyfin — API-compatible fork. Uses the modern `Authorization` request
# header (Jellyfin 10.8+) while still accepting the X-Emby-Token session header
# for compatibility. static=true is supported by Jellyfin too. NOT yet verified
# against a live server — see the module validation gate.
JELLYFIN = BackendSpec(
    name="jellyfin",
    client_name="HyperWall",
    auth_request_header="Authorization",
    token_header="X-Emby-Token",
    requires_static_true=True,
    verified_live=False,
)

_BACKENDS = {b.name: b for b in (EMBY, JELLYFIN)}

DEFAULT_BACKEND = "emby"


def resolve_backend(name: str | None) -> BackendSpec:
    """Return the BackendSpec for a config value, defaulting to Emby.

    Unknown names fall back to Emby with a warning (safe default — the app's
    proven path). Selecting an unverified backend logs a loud warning so it's
    never silently trusted.
    """
    key = (name or DEFAULT_BACKEND).strip().lower()
    spec = _BACKENDS.get(key)
    if spec is None:
        logger.warning(
            "Unknown backend '%s' — falling back to '%s'. Valid: %s",
            name, DEFAULT_BACKEND, ", ".join(sorted(_BACKENDS)),
        )
        return EMBY
    if not spec.verified_live:
        logger.warning(
            "Backend '%s' is NOT yet validated against a live server. "
            "Auth/playback may differ — verify before relying on it.", spec.name,
        )
    return spec


def auth_string(spec: BackendSpec, device_id: str, version: str) -> str:
    """Build the MediaBrowser authorization string (header *value*)."""
    return (
        f'MediaBrowser Client="{spec.client_name}", Device="PC", '
        f'DeviceId="{device_id}", Version="{version}"'
    )


def auth_request_headers(
    spec: BackendSpec, device_id: str, version: str,
) -> dict[str, str]:
    """Headers for the AuthenticateByName POST (Content-Type + auth string)."""
    return {
        "Content-Type": "application/json",
        spec.auth_request_header: auth_string(spec, device_id, version),
    }


def token_headers(spec: BackendSpec, token: str | None) -> dict[str, str]:
    """Per-request auth header carrying the session token."""
    return {spec.token_header: token or ""}
