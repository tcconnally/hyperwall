"""Select an explicitly configured prepared source without replacing its item."""

from __future__ import annotations

from pathlib import PurePosixPath
import math
import re
import struct
from typing import Any


# Appended to a successfully remuxed and validated MP4 before atomic publication.
# MP4 readers ignore the standard free box. Exact bytes also appear in the
# standalone NAS finalizer, whose tests guard this shared on-disk contract.
NORMALIZATION_RECEIPT = (
    struct.pack(">I4s", 64, b"free")
    + b"Hyperwall normalized mp4 v1\n".ljust(56, b"\0")
)


def valid_receipt_response(status: int, content_range: str, body: bytes) -> bool:
    """Accept only the exact final 64 bytes of a nonempty normalized MP4."""
    if status != 206 or body != NORMALIZATION_RECEIPT:
        return False
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
    if match is None:
        return False
    start, end, total = map(int, match.groups())
    return total > 64 and start == total - 64 and end == total - 1


def explicit_receipt_range(status: int, content_range: str) -> str | None:
    """Work around Emby 4.9's observed suffix-range parsing defect.

    It responds to bytes=-64 with bytes 0-64/TOTAL. That is not proof of a
    receipt, but its total permits one bounded request for the exact tail.
    """
    match = re.fullmatch(r"bytes 0-64/(\d+)", content_range)
    if status != 206 or match is None:
        return None
    total = int(match.group(1))
    return f"bytes={total - 64}-{total - 1}" if total > 65 else None


def _server_path(value: Any) -> PurePosixPath | None:
    # These are paths on the Emby server, not paths to resolve on this client.
    if not isinstance(value, str) or not value.startswith("/"):
        return None
    if "\\" in value or "\x00" in value or ".." in value.split("/"):
        return None
    return PurePosixPath(value)


def is_rendition_source(source: Any, root: str) -> bool:
    """Recognize completed, seekable MP4 sources in the dedicated target.

    Directory membership selects a rendition but does not prove good
    interleaving. Prepared audio requires a separate normalization receipt.
    """
    target = _server_path(root)
    if target is None or target == PurePosixPath("/") or not isinstance(source, dict):
        return False
    path = _server_path(source.get("Path"))
    if path is None or path == target or not path.is_relative_to(target):
        return False
    if path.suffix.lower() != ".mp4" or str(source.get("Container", "")).lower() != "mp4":
        return False
    if not isinstance(source.get("Id"), str) or not source["Id"]:
        return False
    if source.get("Protocol") != "File" or source.get("SupportsDirectStream") is not True:
        return False
    if any(source.get(key) for key in (
        "IsRemote", "IsInfiniteStream", "RequiresOpening", "RequiresClosing",
    )):
        return False
    for key in ("Size", "RunTimeTicks"):
        value = source.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            return False
    streams = source.get("MediaStreams")
    return isinstance(streams, list) and any(
        isinstance(stream, dict) and stream.get("Type") == "Video" and stream.get("Codec")
        for stream in streams
    )


def prefer_rendition(item: dict[str, Any], root: str | None) -> dict[str, Any]:
    """Return an enriched copy only when one unambiguous prepared source exists.

    Original identity, favorite/tag metadata and display information remain
    authoritative. Missing or ambiguous renditions keep the original item in
    the library and preserve its ordinary playback behavior.
    """
    if not root:
        return item
    sources = item.get("MediaSources")
    if not isinstance(sources, list):
        return item
    candidates = [source for source in sources if is_rendition_source(source, root)]
    if len(candidates) != 1:
        return item
    source = candidates[0]
    enriched = dict(item)
    enriched["MediaSources"] = [dict(source)]
    enriched["MediaStreams"] = source["MediaStreams"]
    enriched["_hyperwall_prepared"] = False
    enriched["_hyperwall_media_source_id"] = source["Id"]
    return enriched
