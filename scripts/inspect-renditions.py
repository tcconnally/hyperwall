#!/usr/bin/env python3
"""Inspect source discovery without opening media or printing credentials/titles."""

from __future__ import annotations

import argparse
from configparser import ConfigParser
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hyperwall.renditions import is_rendition_source


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--item-id", required=True)
    parser.add_argument("--rendition-root", default="/hyperwall/mv")
    args = parser.parse_args(argv)
    config = ConfigParser()
    config.read(args.config)
    base = config.get("Login", "server_url").rstrip("/")

    def request(path, *, body=None, headers=None):
        req = Request(base + path, data=None if body is None else json.dumps(body).encode(),
                      headers=headers or {})
        with urlopen(req, timeout=30) as response:
            return json.load(response)

    try:
        auth = request("/Users/AuthenticateByName", body={
            "Username": config.get("Login", "username"),
            "Pw": config.get("Login", "password"),
        }, headers={
            "Content-Type": "application/json",
            "X-Emby-Authorization": 'MediaBrowser Client="HyperwallInspect", Device="ReadOnly", '
                                    'DeviceId="hyperwall-rendition-inspect", Version="1"',
        })
        headers = {"X-Emby-Token": auth["AccessToken"]}
        user_id = auth["User"]["Id"]
        item = request(f"/Users/{user_id}/Items/{args.item_id}", headers=headers)
        info = request(f"/Items/{args.item_id}/PlaybackInfo?" + urlencode({"UserId": user_id}),
                       headers=headers)

        def summarize(payload):
            sources = payload.get("MediaSources") or []
            return {
                "source_count": len(sources),
                "rendition_source_count": sum(is_rendition_source(s, args.rendition_root) for s in sources),
                "sources": [{
                    "media_source_id": s.get("Id"), "protocol": s.get("Protocol"),
                    "container": s.get("Container"), "supports_direct_stream": s.get("SupportsDirectStream"),
                    "rendition_candidate": is_rendition_source(s, args.rendition_root),
                    "duration_s": (s.get("RunTimeTicks") or 0) / 10_000_000,
                    "size_bytes": s.get("Size"),
                    "video": [{k: v.get(k) for k in ("Codec", "Width", "Height", "AverageFrameRate")}
                              for v in s.get("MediaStreams", []) if v.get("Type") == "Video"],
                } for s in sources],
            }

        # Compare the actual bulk-library shape as well as detail/PlaybackInfo.
        listing = request(f"/Users/{user_id}/Items?" + urlencode({
            "Ids": args.item_id, "Fields": "MediaSources,MediaStreams,UserData,Tags",
        }), headers=headers)
        listed = (listing.get("Items") or [{}])[0]
        print(json.dumps({"item_id": args.item_id, "list": summarize(listed),
                          "detail": summarize(item), "playback_info": summarize(info)}, indent=2))
        return 0
    except Exception as exc:
        # Network exception strings can contain URLs; output only a safe class/status.
        print(json.dumps({"error": type(exc).__name__,
                          "http_status": exc.code if isinstance(exc, HTTPError) else None}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
