#!/usr/bin/env python3
"""Summarize local Hyperwall JSONL files. No network or third-party packages."""
import argparse
import json
from pathlib import Path


def summarize(paths):
    out = {"samples": 0, "sessions": 0, "maximum_cells": 0,
           "max_loop_lag_ms": 0, "max_cpu_percent_one_core_100": 0,
           "peak_rss_mib": 0, "escape_hidden_ms": [], "decoders": [],
           "observed_items": 0, "max_recorded_freezes_per_cell": 0,
           "max_video_drops_per_cell": 0, "max_decoder_drops_per_cell": 0,
           "buffering_cell_samples": 0}
    items, decoders = set(), set()
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try: row = json.loads(line)
                except ValueError: continue  # a hard exit can leave a partial final line
                if row.get("event") == "start": out["sessions"] += 1
                if row.get("event") == "escape":
                    out["escape_hidden_ms"].append(row.get("windows_hidden_ms"))
                if row.get("event") != "sample": continue
                out["samples"] += 1
                cells = row.get("cells", [])
                out["maximum_cells"] = max(out["maximum_cells"], len(cells))
                out["max_loop_lag_ms"] = max(out["max_loop_lag_ms"], row.get("loop_lag_ms", {}).get("max", 0))
                resources = row.get("resources", {})
                for key in ("peak_rss_mib", "cpu_percent_one_core_100"):
                    dest = "max_" + key if key.startswith("cpu") else key
                    out[dest] = max(out[dest], resources.get(key, 0))
                for cell in cells:
                    if cell.get("item_id"): items.add(str(cell["item_id"]))
                    decoder = cell.get("media", {}).get("hwdec-current")
                    if decoder: decoders.add(str(decoder))
                    out["max_recorded_freezes_per_cell"] = max(out["max_recorded_freezes_per_cell"], cell.get("freezes", 0))
                    out["buffering_cell_samples"] += bool(cell.get("buffering"))
                    counters = cell.get("counters", {})
                    for source, dest in (("frame-drop-count", "max_video_drops_per_cell"),
                                         ("decoder-frame-drop-count", "max_decoder_drops_per_cell")):
                        out[dest] = max(out[dest], counters.get(source, 0))
    out["observed_items"], out["decoders"] = len(items), sorted(decoders)
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args()
    paths = args.files or sorted((Path(__file__).resolve().parents[1] / "logs" / "telemetry").glob("*.jsonl*"))
    print(json.dumps(summarize(paths), indent=2))
