"""
HyperWall v8 -- max concurrent stress test.

Picks the N highest-bitrate items from the configured libraries, spins up N
concurrent off-screen libmpv decoders against the production URL pipeline
(2-tier: DIRECT for <=1080p, TRANSCODE for >1080p), runs for stress_secs
seconds, and reports whether all N held steady playback.

Decode path: vo=null + hwdec=auto-copy. Exercises NVDEC, skips render. The
5070 Ti's render pipeline is not the bottleneck for 12 cells of 1080p
compositing -- NVDEC throughput and network/server I/O are what we need to
prove out.

Usage:
    py test_max_concurrent.py [--cells N] [--stress-secs S] [--libraries lib1,...]
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import statistics
import subprocess
import sys
import threading
import time

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

WALL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(WALL_DIR, "config.ini")

os.environ["PATH"] = WALL_DIR + os.pathsep + os.environ.get("PATH", "")
# Suppress hyperwall_v8's file-log + priority side-effects when imported as a
# library (see hyperwall_v8 logger setup). Must be set before the import.
os.environ.setdefault("HYPERWALL_NO_LOG_SETUP", "1")
import mpv  # noqa: E402

# Production parity: pull MPV_OPTS straight from the packaged runtime so the
# harness tracks the same tuning the wall actually runs.
from hyperwall.perf import MPV_OPTS, apply_perf_env  # noqa: E402

_BASE_OPTS = apply_perf_env(MPV_OPTS)
_BASE_OPTS.update(dict(
    vo="null",
    ao="null",
    # vo=null can't hold a d3d11 render surface; force hwdec=auto-copy unless
    # the caller explicitly set HYPERWALL_HWDEC (in which case respect it
    # but it must be a *-copy variant or 'no' to work with vo=null).
    hwdec=os.environ.get("HYPERWALL_HWDEC") or "auto-copy",
))
# Drop wall-specific keys that don't apply to off-screen tests.
for _k in ("audio_client_name",):
    _BASE_OPTS.pop(_k, None)


# ── Emby helpers (mirror of test_codec_matrix) ───────────────────────────────
class Emby:
    def __init__(self, url, user, pw):
        self.url, self.user, self.pw = url.rstrip("/"), user, pw
        self.token = self.uid = None
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "HyperWall-stress/8.0"

    def auth(self):
        device_id = f"hwstress-{os.urandom(4).hex()}"
        r = self.s.post(
            f"{self.url}/Users/AuthenticateByName",
            headers={"Content-Type": "application/json",
                     "X-Emby-Authorization": (
                         f'MediaBrowser Client="HyperWall-stress", Device="PC", '
                         f'DeviceId="{device_id}", Version="8.0"')},
            json={"Username": self.user, "Pw": self.pw},
            timeout=10, verify=False,
        )
        r.raise_for_status()
        d = r.json()
        self.token, self.uid = d["AccessToken"], d["User"]["Id"]

    def get(self, path, **kw):
        return self.s.get(f"{self.url}{path}",
                          headers={"X-Emby-Token": self.token},
                          verify=False, **kw)

    def libraries(self):
        return self.get(f"/Users/{self.uid}/Views", timeout=10).json().get("Items", [])

    def items(self, library_id):
        return self.get(
            f"/Users/{self.uid}/Items",
            params={"ParentId": library_id, "Recursive": "true",
                    "IncludeItemTypes": "Video,MusicVideo,Movie,Episode",
                    "Fields": "MediaSources,MediaStreams,Container",
                    "Limit": "10000"},
            timeout=30,
        ).json().get("Items", [])


def stream_url(base, item, key, force_transcode=False, always_direct=False):
    """Mirror of WallController._build_url. Production is always-DIRECT;
    always_direct kept as no-op for backward compat with the --force-direct
    flag (which is now redundant but doesn't hurt)."""
    iid = item["Id"]
    import uuid
    if force_transcode:
        sid = uuid.uuid4().hex
        url = (f"{base}/Videos/{iid}/master.m3u8?api_key={key}"
               f"&VideoCodec=h264&AudioCodec=aac&MaxAudioChannels=2"
               f"&MaxHeight=1080&MaxWidth=1920&VideoBitrate=12000000"
               f"&PlaySessionId={sid}")
        return url, "TRANSCODE", sid
    return f"{base}/Videos/{iid}/stream?api_key={key}&static=true", "DIRECT", None


def src_bitrate(item):
    src = (item.get("MediaSources") or [{}])[0]
    return src.get("Bitrate") or 0


def src_resolution(item):
    src = (item.get("MediaSources") or [{}])[0]
    streams = src.get("MediaStreams") or item.get("MediaStreams") or []
    v = next((s for s in streams if s.get("Type") == "Video"), {}) or {}
    return v.get("Width") or 0, v.get("Height") or 0, (v.get("Codec") or "?").lower()


# ── Per-cell decode worker ───────────────────────────────────────────────────
class CellWorker:
    def __init__(self, idx: int, item: dict, url: str, tier: str, sid: str | None = None):
        self.idx, self.item, self.url, self.tier, self.sid = idx, item, url, tier, sid
        self.start_t       = None
        self.first_frame_t = None
        self.last_pos      = 0.0
        self.pos_samples: list[tuple[float, float]] = []  # (wall_t, pos)
        self.eof_reason    = None
        self.error_lines: list[str] = []
        self._mpv          = None

    def start(self):
        def log_cb(level, _comp, msg):
            if level in ("fatal", "error"):
                self.error_lines.append(msg.strip()[:200])

        # Production-parity opts (imported from hyperwall_v8); harness-only
        # overrides for off-screen + simpler logging.
        opts = dict(_BASE_OPTS)
        opts["msg_level"] = "all=warn"
        self._mpv = mpv.MPV(log_handler=log_cb, **opts)

        @self._mpv.property_observer("time-pos")
        def _on_time(_n, value):
            if value is None: return
            now = time.time()
            self.last_pos = float(value)
            self.pos_samples.append((now - self.start_t, self.last_pos))
            if self.first_frame_t is None and value > 0.05:
                self.first_frame_t = now - self.start_t

        @self._mpv.event_callback("end-file")
        def _on_eof(ev):
            try:
                self.eof_reason = str(ev.event.get("reason", "eof"))
            except Exception:
                self.eof_reason = "eof"

        self.start_t = time.time()
        self._mpv.play(self.url)

    def stop(self):
        try: self._mpv.terminate()
        except Exception: pass

    def status(self, run_secs: float) -> dict:
        # Compute progress consistency: did time-pos advance steadily?
        # Measure as ratio: actual playback advance / wall-clock elapsed.
        if len(self.pos_samples) >= 2:
            t0, p0 = self.pos_samples[0]
            t1, p1 = self.pos_samples[-1]
            wall_elapsed = max(0.001, t1 - t0)
            playback_elapsed = max(0.0, p1 - p0)
            ratio = playback_elapsed / wall_elapsed
        else:
            ratio = 0.0
        passed = (
            self.first_frame_t is not None
            and self.first_frame_t < 8.0
            and self.last_pos >= run_secs * 0.7
            and self.eof_reason in (None, "eof")
            and ratio >= 0.85   # within 15% of real-time
        )
        return {
            "idx": self.idx,
            "item": self.item.get("Name", "?"),
            "tier": self.tier,
            "first_frame_s": self.first_frame_t,
            "last_pos_s": self.last_pos,
            "playback_ratio": ratio,
            "eof_reason": self.eof_reason,
            "errors": self.error_lines[:5],
            "passed": passed,
        }


def gpu_snapshot():
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            text=True, timeout=5, creationflags=0x08000000,
        ).strip()
        gpu_u, mem_u, mem_used, mem_total, temp = out.split(", ")
        return f"GPU {gpu_u}%  VRAM {mem_used}/{mem_total} MB  ({mem_u}% bw)  {temp}C"
    except Exception as e:
        return f"nvidia-smi error: {e}"


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cells", type=int, default=12)
    ap.add_argument("--stress-secs", type=float, default=30.0)
    ap.add_argument("--libraries", default=None)
    ap.add_argument("--start-stagger-ms", type=int, default=300,
                    help="Match production STREAM_START_STAGGER_MS")
    ap.add_argument("--prefer-largest", action="store_true", default=True,
                    help="Pick by bitrate descending; --no-prefer-largest for random sample")
    ap.add_argument("--force-direct", action="store_true",
                    help="(legacy, no-op now that prod is always-DIRECT)")
    ap.add_argument("--random", action="store_true",
                    help="Pick a random sample of N cells instead of the highest-"
                         "bitrate ones — represents typical wall load")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    import random as _rand
    _rand.seed(args.seed)

    cfg = configparser.ConfigParser(); cfg.read(CONFIG)
    e = Emby(cfg.get("Login", "server_url"),
             cfg.get("Login", "username"),
             cfg.get("Login", "password"))
    print(f"Auth -> {e.url}")
    e.auth()

    libs = e.libraries()
    if args.libraries:
        wanted = set(s.strip() for s in args.libraries.split(","))
        libs = [l for l in libs if l["Name"] in wanted]
    libs = [l for l in libs if l.get("CollectionType") in (None, "movies", "tvshows",
                                                           "homevideos", "musicvideos", "mixed")]
    print(f"Libraries ({len(libs)}): {', '.join(l['Name'] for l in libs)}")

    items = []
    for lib in libs:
        items.extend(e.items(lib["Id"]))
    items = [it for it in items if src_bitrate(it) > 0]

    print(f"Total items with known bitrate: {len(items)}")
    if not items:
        sys.exit("no items to test")

    if args.random:
        selected = _rand.sample(items, min(args.cells, len(items)))
        mode = "RANDOM (typical-load simulation)"
    else:
        items.sort(key=src_bitrate, reverse=True)
        selected = items[:args.cells]
        mode = "TOP-N BY BITRATE (worst-case stress)"
    print(f"Selection mode: {mode}")
    print()
    print(f"Top {len(selected)} by source bitrate:"
          + ("  (force-direct mode)" if args.force_direct else ""))
    print("-" * 110)
    for i, it in enumerate(selected, 1):
        w, h, vc = src_resolution(it)
        br_mbps = src_bitrate(it) / 1e6
        url, tier, _sid = stream_url(e.url, it, e.token, always_direct=args.force_direct)
        print(f"  {i:2d}  {br_mbps:5.1f} Mbps  {vc:6} {w}x{h:4d}  [{tier:9}]  {it['Name'][:60]}")
    print("-" * 110)
    total_mbps = sum(src_bitrate(it) for it in selected) / 1e6
    print(f"Aggregate source bitrate (DIRECT-only ceiling): {total_mbps:.1f} Mbps")
    print(f"GPU pre-test: {gpu_snapshot()}")
    print()

    # Build workers
    workers = []
    for i, it in enumerate(selected):
        url, tier, sid = stream_url(e.url, it, e.token, always_direct=args.force_direct)
        workers.append(CellWorker(i + 1, it, url, tier, sid))

    # Stagger-start identical to production
    print(f"Starting {len(workers)} cells with {args.start_stagger_ms}ms stagger...")
    for w in workers:
        w.start()
        time.sleep(args.start_stagger_ms / 1000.0)
    print(f"All started. Holding for {args.stress_secs}s...")

    # Hold and sample GPU mid-test
    midpoint = args.stress_secs / 2
    time.sleep(midpoint)
    print(f"GPU mid-test:  {gpu_snapshot()}")
    time.sleep(args.stress_secs - midpoint)
    print(f"GPU end-test:  {gpu_snapshot()}")

    # Tear down: stop mpv, then notify Emby on each session so transcoders
    # don't linger between test runs.
    for w in workers:
        w.stop()
    for w in workers:
        if w.sid:
            try:
                e.s.post(f"{e.url}/Sessions/Playing/Stopped",
                         headers={"X-Emby-Token": e.token,
                                  "Content-Type": "application/json"},
                         json={"ItemId": w.item["Id"],
                               "PlaySessionId": w.sid,
                               "PositionTicks": 0},
                         verify=False, timeout=5)
            except Exception:
                pass

    # Aggregate
    print()
    print("=" * 110)
    print(f"{'#':>3} {'tier':9} {'first':>7} {'pos':>7} {'rt%':>5} {'eof':>10} {'res':10} {'name'}")
    print("=" * 110)
    statuses = [w.status(args.stress_secs) for w in workers]
    for st in statuses:
        ff = f"{st['first_frame_s']:.2f}" if st['first_frame_s'] else "  -  "
        flag = "PASS" if st['passed'] else "FAIL"
        rt = int(st['playback_ratio'] * 100)
        wkr = workers[st['idx'] - 1]
        wd, hd, _vc = src_resolution(wkr.item)
        print(f"{st['idx']:3d} {st['tier']:9} {ff:>7} {st['last_pos_s']:7.2f} {rt:>4}% "
              f"{(st['eof_reason'] or 'live'):>10} {wd}x{hd:4d}  {flag}  {st['item'][:50]}")

    passed = sum(1 for s in statuses if s["passed"])
    print("=" * 110)
    print(f"  RESULT: {passed}/{len(statuses)} cells held steady playback for >={args.stress_secs * 0.7:.0f}s")

    # Failure detail
    fails = [s for s in statuses if not s["passed"]]
    if fails:
        print()
        print("Failure detail:")
        for s in fails:
            print(f"  cell {s['idx']} [{s['tier']}]: {s['item'][:60]}")
            print(f"    first_frame={s['first_frame_s']}  last_pos={s['last_pos_s']:.1f}  "
                  f"ratio={s['playback_ratio']:.2f}  eof={s['eof_reason']}")
            for ln in s["errors"][:3]:
                print(f"    err: {ln}")

    # Persist
    out = os.path.join(WALL_DIR, "test_max_concurrent_results.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"args": vars(args), "statuses": statuses}, f, indent=2)
    print(f"\nJSON: {out}")
    sys.exit(0 if passed == len(statuses) else 1)


if __name__ == "__main__":
    main()
