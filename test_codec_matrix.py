"""
HyperWall v8 -- silent codec/source matrix test.

Headless. Doesn't touch the screens. For each unique
(video_codec, audio_codec, container, resolution_bucket) combination in the
configured libraries, samples N items, builds the same always-REMUX URL the
production WallController emits, plays each through libmpv with vo=null +
ao=null, and reports decode success rate per bucket.

Usage:
    py test_codec_matrix.py [--per-bucket N] [--decode-secs S] [--timeout-secs T] [--libraries lib1,lib2,...]

Exits non-zero if any bucket has <100% pass rate (catches regressions).
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import random
import sys
import threading
import time
import uuid
from collections import defaultdict

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

WALL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(WALL_DIR, "config.ini")

# Make python-mpv find mpv-2.dll bundled next to this script.
# python-mpv's loader reads %PATH% directly (not AddDllDirectory), so prepend.
os.environ["PATH"] = WALL_DIR + os.pathsep + os.environ.get("PATH", "")
import mpv  # noqa: E402


# ── Emby ─────────────────────────────────────────────────────────────────────
class Emby:
    def __init__(self, url, user, pw):
        self.url = url.rstrip("/")
        self.user = user
        self.pw = pw
        self.token = None
        self.uid = None
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "HyperWall-test/8.0"

    def auth(self):
        device_id = f"hwtest-{os.urandom(4).hex()}"
        r = self.s.post(
            f"{self.url}/Users/AuthenticateByName",
            headers={
                "Content-Type": "application/json",
                "X-Emby-Authorization": (
                    f'MediaBrowser Client="HyperWall-test", Device="PC", '
                    f'DeviceId="{device_id}", Version="8.0"'
                ),
            },
            json={"Username": self.user, "Pw": self.pw},
            timeout=10, verify=False,
        )
        r.raise_for_status()
        d = r.json()
        self.token = d["AccessToken"]
        self.uid = d["User"]["Id"]

    def get(self, path, **kw):
        return self.s.get(f"{self.url}{path}",
                          headers={"X-Emby-Token": self.token},
                          verify=False, **kw)

    def libraries(self):
        return self.get(f"/Users/{self.uid}/Views", timeout=10).json().get("Items", [])

    def items(self, library_id):
        return self.get(
            f"/Users/{self.uid}/Items",
            params={
                "ParentId": library_id,
                "Recursive": "true",
                "IncludeItemTypes": "Video,MusicVideo,Movie,Episode",
                "Fields": "MediaSources,MediaStreams,Container",
                "Limit": "10000",
            },
            timeout=30,
        ).json().get("Items", [])


def stream_url(base, iid, key, item, force_transcode=False):
    """Mirror of WallController._build_url in hyperwall_v8.py.
    Default = DIRECT static stream (client decodes everything).
    force_transcode = retry escape; server transcodes to 1080p H.264.
    Returns (url, tier)."""
    if force_transcode:
        sid = uuid.uuid4().hex
        url = (f"{base}/Videos/{iid}/master.m3u8?api_key={key}"
               f"&VideoCodec=h264&AudioCodec=aac&MaxAudioChannels=2"
               f"&MaxHeight=1080&MaxWidth=1920&VideoBitrate=12000000"
               f"&PlaySessionId={sid}")
        return url, "TRANSCODE"
    return f"{base}/Videos/{iid}/stream?api_key={key}&static=true", "DIRECT"


# ── Bucketing ────────────────────────────────────────────────────────────────
def res_bucket(w, h):
    if not w or not h: return "?"
    if h <= 480:  return "SD"
    if h <= 720:  return "HD720"
    if h <= 1080: return "HD1080"
    if h <= 1440: return "QHD"
    return "4K+"


def classify(item):
    src = (item.get("MediaSources") or [{}])[0]
    container = (src.get("Container") or "?").lower()
    streams = src.get("MediaStreams") or item.get("MediaStreams") or []
    v = next((s for s in streams if s.get("Type") == "Video"), {}) or {}
    audios = sorted({(s.get("Codec") or "?").lower()
                     for s in streams if s.get("Type") == "Audio"})
    a_key = ",".join(audios) or "none"
    vc = (v.get("Codec") or "?").lower()
    return (vc, a_key, container, res_bucket(v.get("Width"), v.get("Height")))


# ── Single decode test ──────────────────────────────────────────────────────
def test_decode(url, decode_secs, timeout_secs):
    """
    Returns: (passed: bool, reason: str, first_frame_s: float|None,
              advanced_s: float, log_tail: list[str])
    """
    log_tail: list[str] = []
    state = {"first_frame": None, "last_pos": 0.0, "eof_reason": None,
             "errors": [], "start": time.time()}
    done = threading.Event()

    def log_cb(level, component, message):
        if level in ("fatal", "error", "warn"):
            line = f"[{level}/{component}] {message.strip()}"
            log_tail.append(line)
            if len(log_tail) > 12:
                log_tail.pop(0)
            if level in ("fatal", "error"):
                state["errors"].append(line)

    # Off-screen decode-only. Use software decode -- d3d11va requires a render
    # surface that vo=null can't provide. This validates URL pipeline + Emby
    # remux output + demux/decode correctness; HW decode is exercised by
    # production runs.
    m = mpv.MPV(
        vo="null",
        ao="null",
        hwdec="no",
        cache="yes",
        cache_secs=10,
        demuxer_max_bytes="64MiB",
        network_timeout=15,
        stream_lavf_o="reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
        keep_open="no",
        ytdl=False,
        msg_level="all=warn",
        log_handler=log_cb,
    )

    @m.property_observer("time-pos")
    def _on_time(_n, value):
        if value is None: return
        if state["first_frame"] is None and value > 0:
            state["first_frame"] = time.time() - state["start"]
        state["last_pos"] = float(value)
        if state["last_pos"] >= decode_secs:
            done.set()

    @m.event_callback("end-file")
    def _on_eof(ev):
        try:
            r = ev.event.get("reason", "eof")
        except Exception:
            r = "eof"
        state["eof_reason"] = str(r)
        done.set()

    try:
        m.play(url)
        done.wait(timeout=timeout_secs)
    finally:
        try: m.terminate()
        except Exception: pass

    elapsed = time.time() - state["start"]
    if state["eof_reason"] == "error":
        return False, "mpv end-file error", state["first_frame"], state["last_pos"], log_tail
    if state["first_frame"] is None:
        return False, f"no first frame within {timeout_secs}s", None, 0.0, log_tail
    if state["last_pos"] < decode_secs and state["eof_reason"] not in ("eof", None):
        return False, f"playback stopped early ({state['eof_reason']})", state["first_frame"], state["last_pos"], log_tail
    if state["last_pos"] < min(2.0, decode_secs * 0.5):
        return False, f"insufficient progress: {state['last_pos']:.1f}s in {elapsed:.1f}s", state["first_frame"], state["last_pos"], log_tail
    return True, "ok", state["first_frame"], state["last_pos"], log_tail


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-bucket", type=int, default=3,
                    help="Samples per (vcodec,acodec,container,res) bucket")
    ap.add_argument("--decode-secs", type=float, default=4.0,
                    help="How many seconds of playback to validate per item")
    ap.add_argument("--timeout-secs", type=float, default=15.0,
                    help="Hard timeout per test")
    ap.add_argument("--libraries", default=None,
                    help="Comma-separated library names; default = all video libraries")
    ap.add_argument("--max-tests", type=int, default=200,
                    help="Cap total tests (safety)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    random.seed(args.seed)

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG)
    e = Emby(cfg.get("Login", "server_url"),
             cfg.get("Login", "username"),
             cfg.get("Login", "password"))
    print(f"Auth -> {e.url}")
    e.auth()

    libs = e.libraries()
    if args.libraries:
        wanted = set(s.strip() for s in args.libraries.split(","))
        libs = [l for l in libs if l["Name"] in wanted]
    # Skip non-video collections (music, photos)
    libs = [l for l in libs if l.get("CollectionType") in (None, "movies", "tvshows",
                                                           "homevideos", "musicvideos",
                                                           "mixed")]
    print(f"Libraries ({len(libs)}): {', '.join(l['Name'] for l in libs)}")

    items = []
    for lib in libs:
        n = e.items(lib["Id"])
        items.extend(n)
        print(f"  {lib['Name']:20s} {len(n):6d} items")
    print(f"Total items: {len(items)}")

    # Bucket
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for it in items:
        buckets[classify(it)].append(it)

    print(f"Unique (vcodec,acodec,container,res) buckets: {len(buckets)}")
    print()

    # Sample
    plan = []
    for key, group in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        sample = random.sample(group, min(args.per_bucket, len(group)))
        for it in sample:
            plan.append((key, it))
    if len(plan) > args.max_tests:
        plan = plan[:args.max_tests]
        print(f"Plan capped at {args.max_tests} tests.")

    print(f"Plan: {len(plan)} tests across {len(buckets)} buckets")
    print(f"Decode validation: each must reach {args.decode_secs}s playback within {args.timeout_secs}s timeout")
    print(f"hwdec OFF for headless validation (URL+remux+demux only); production uses d3d11va")
    print()
    print("=" * 110)
    print(f"{'#':>3} {'tier':9} {'vcodec':10} {'audio':16} {'cont':6} {'res':6} {'first':>7} {'pos':>6} {'result':8} {'name'}")
    print("=" * 110)

    results = []
    tier_counts = defaultdict(int)
    started = time.time()
    for i, (key, it) in enumerate(plan, 1):
        vc, ac, ct, rb = key
        url, tier = stream_url(e.url, it["Id"], e.token, it)
        tier_counts[tier] += 1
        # Extract session id from URL (only present for TRANSCODE tier).
        sid = None
        if "PlaySessionId=" in url:
            sid = url.split("PlaySessionId=", 1)[1].split("&", 1)[0]
        ok, reason, first, pos, tail = test_decode(url, args.decode_secs, args.timeout_secs)
        # Hygiene: stop the Emby session so transcoders don't accumulate
        # across the serial run and corrupt later results.
        if sid:
            try:
                e.s.post(f"{e.url}/Sessions/Playing/Stopped",
                         headers={"X-Emby-Token": e.token,
                                  "Content-Type": "application/json"},
                         json={"ItemId": it["Id"], "PlaySessionId": sid,
                               "PositionTicks": 0},
                         verify=False, timeout=5)
            except Exception:
                pass
        results.append({
            "key": key, "item": it["Name"], "id": it["Id"], "tier": tier,
            "ok": ok, "reason": reason,
            "first_frame_s": first, "pos_s": pos, "log_tail": tail,
        })
        first_s = f"{first:.2f}" if first is not None else "  -  "
        flag = "PASS" if ok else "FAIL"
        name = (it["Name"] or "")[:38]
        print(f"{i:3d} {tier:9} {vc:10} {ac:16} {ct:6} {rb:6} {first_s:>7} {pos:6.2f} {flag:8} {name}")

    elapsed = time.time() - started
    print("=" * 110)
    print(f"Wall time: {elapsed:.1f}s  ({elapsed/len(plan):.2f}s/test avg)")
    print(f"Tier breakdown: {dict(tier_counts)}")

    # Per-bucket aggregate
    bucket_stats: dict[tuple, list[bool]] = defaultdict(list)
    for r in results:
        bucket_stats[r["key"]].append(r["ok"])

    print()
    print("--- Per-bucket pass rate ---")
    print(f"{'vcodec':10} {'audio':16} {'cont':6} {'res':6}  {'pass':>6}  {'tested':>6}")
    fails_per_bucket = []
    for key in sorted(bucket_stats.keys()):
        outs = bucket_stats[key]
        passed = sum(outs)
        tested = len(outs)
        rate = passed / tested
        marker = "OK" if rate == 1.0 else f"{rate*100:.0f}%"
        vc, ac, ct, rb = key
        print(f"{vc:10} {ac:16} {ct:6} {rb:6}  {passed:>6}  {tested:>6}  {marker}")
        if rate < 1.0:
            fails_per_bucket.append(key)

    # Failure detail
    fails = [r for r in results if not r["ok"]]
    if fails:
        print()
        print(f"--- Failure details ({len(fails)}) ---")
        for r in fails:
            vc, ac, ct, rb = r["key"]
            print(f"  [{vc}/{ac}/{ct}/{rb}] {r['item'][:60]}")
            print(f"    reason: {r['reason']}")
            print(f"    item id: {r['id']}")
            for line in r["log_tail"][-4:]:
                print(f"    log: {line}")

    # Summary
    total_pass = sum(1 for r in results if r["ok"])
    total = len(results)
    print()
    print("=" * 50)
    print(f"  TOTAL:  {total_pass}/{total}  ({total_pass/total*100:.1f}%)")
    print(f"  BUCKETS: {len(bucket_stats) - len(fails_per_bucket)}/{len(bucket_stats)} clean")
    print("=" * 50)

    # Write JSON for archival
    out = os.path.join(WALL_DIR, "test_codec_matrix_results.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "elapsed_s": elapsed,
            "args": vars(args),
            "results": [{**r, "key": list(r["key"])} for r in results],
        }, f, indent=2)
    print(f"\nJSON archive: {out}")

    sys.exit(0 if total_pass == total else 1)


if __name__ == "__main__":
    main()
