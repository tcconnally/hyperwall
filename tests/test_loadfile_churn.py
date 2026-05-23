"""
Silent test: rapid loadfile churn on a single mpv instance.

Reproduces the live regression where filter-change (F/A) caused
EOF-before-first-frame storms. Initial wall start uses fresh mpv instances
and works; F/A reuses existing instances via loadfile and fails. This test
spins up one mpv per "cell", does a sequence of loadfile-replace operations
on each instance with sub-second spacing, and reports whether each load
actually plays at least N seconds.

Pass = every load plays the target duration. Fail = EOF-before-first-frame
or partial playback indicates the loadfile sequence broke mpv's state.

Usage:
    py test_loadfile_churn.py [--cells N] [--cycles K] [--play-secs S]
"""
from __future__ import annotations

import argparse, configparser, os, sys, time, threading, random
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Package-level import smoke test (fail fast) ──────────────────────────────
# Ensures the hyperwall package + core modules are importable and not broken
# before expensive stress / churn logic runs. Catches packaging, syntax, or
# circular-import regressions immediately.
import hyperwall
from hyperwall.cell import VideoCell
from hyperwall.perf import MPV_OPTS, MAX_RETRIES
from hyperwall.emby import EmbyAPISession


WALL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["PATH"] = WALL_DIR + os.pathsep + os.environ.get("PATH", "")
import mpv  # noqa


def auth(cfg):
    s = requests.Session()
    auth_h = ('MediaBrowser Client="hwchurn", Device="PC", '
              'DeviceId="hwchurn", Version="8.0"')
    r = s.post(f'{cfg["url"]}/Users/AuthenticateByName',
               headers={'Content-Type': 'application/json',
                        'X-Emby-Authorization': auth_h},
               json={'Username': cfg['user'], 'Pw': cfg['pw']},
               timeout=10, verify=False)
    r.raise_for_status()
    d = r.json()
    return s, d['AccessToken'], d['User']['Id']


def fetch_items(s, base, uid, tok, n=200):
    """Return n random video items from any video library."""
    items = []
    for v in s.get(f'{base}/Users/{uid}/Views',
                   headers={'X-Emby-Token': tok}, verify=False).json()['Items']:
        if v.get('CollectionType') in (None, 'movies', 'tvshows',
                                       'homevideos', 'musicvideos', 'mixed'):
            r = s.get(f'{base}/Users/{uid}/Items',
                      params={'ParentId': v['Id'], 'Recursive': 'true',
                              'IncludeItemTypes': 'Video,Movie,Episode,MusicVideo',
                              'Limit': '500'},
                      headers={'X-Emby-Token': tok}, verify=False).json()
            items.extend(r.get('Items', []))
    random.shuffle(items)
    return items[:n]


class Cell:
    def __init__(self, idx):
        self.idx = idx
        self.mpv = mpv.MPV(
            vo='null', ao='null', hwdec='auto-copy',
            cache='yes', cache_secs=10,
            demuxer_max_bytes='128MiB',
            keep_open='no', ytdl=False,
            msg_level='all=warn',
        )
        self._lock = threading.Lock()
        self._reset_load_state()

        @self.mpv.property_observer('time-pos')
        def _on_time(_n, value):
            if value is None: return
            with self._lock:
                self.last_pos = float(value)
                if value > 0.05 and self.first_frame_t is None:
                    self.first_frame_t = time.time() - self.load_t

        @self.mpv.event_callback('end-file')
        def _on_eof(ev):
            try: r = ev.event.get('reason', 'eof')
            except: r = 'eof'
            with self._lock:
                self.eof_reason = str(r)

    def _reset_load_state(self):
        self.load_t = time.time()
        self.first_frame_t = None
        self.last_pos = 0.0
        self.eof_reason = None

    def load_via_replace(self, url):
        """The OLD path: bare loadfile-replace. Reproduces the bug."""
        with self._lock:
            self._reset_load_state()
        self.mpv.loadfile(url, 'replace')

    def load_via_stop_then_load(self, url):
        """The FIX: explicit stop + clear + loadfile. Should hold up under churn."""
        with self._lock:
            self._reset_load_state()
        self.mpv.command('stop')
        self.mpv.command('playlist-clear')
        self.mpv['pause'] = False
        self.mpv['mute'] = True
        self.mpv.command('loadfile', url)

    def status(self):
        with self._lock:
            return self.first_frame_t, self.last_pos, self.eof_reason

    def stop(self):
        try: self.mpv.terminate()
        except Exception: pass


def run_cycle(cells: list[Cell], items: list[dict], base: str, tok: str,
              play_secs: float, mode: str, hold_first: float = 0.0):
    """Each cell does one load+wait+report. hold_first=N first plays N seconds
    of an initial item before swapping — closer reproduction of the live bug
    where cells are already mid-decode when filter change fires."""
    chosen = random.sample(items, len(cells))
    urls = [f'{base}/Videos/{it["Id"]}/stream?api_key={tok}&static=true'
            for it in chosen]

    # OPTIONAL: pre-load and hold so cells are actively decoding when the
    # mode-under-test triggers the swap (mirrors live filter-change scenario).
    if hold_first > 0:
        prefill = random.sample(items, len(cells))
        for c, it in zip(cells, prefill):
            pre_url = f'{base}/Videos/{it["Id"]}/stream?api_key={tok}&static=true'
            c.load_via_stop_then_load(pre_url)
            time.sleep(0.3)
        time.sleep(hold_first)

    # Now do the actual swap with the mode under test
    for c, u in zip(cells, urls):
        if mode == 'replace':
            c.load_via_replace(u)
        else:
            c.load_via_stop_then_load(u)
        time.sleep(0.3)

    # Wait for play_secs to elapse
    time.sleep(play_secs)

    # Collect status
    results = []
    for c, it in zip(cells, chosen):
        ff, pos, eof = c.status()
        ok = (ff is not None and ff < 5.0
              and pos >= play_secs * 0.5
              and eof in (None, 'eof'))
        results.append((c.idx, ff, pos, eof, ok, it['Name'][:40]))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cells', type=int, default=8)
    ap.add_argument('--cycles', type=int, default=4,
                    help='How many filter-change-equivalents to simulate')
    ap.add_argument('--play-secs', type=float, default=4.0)
    ap.add_argument('--mode', choices=('replace', 'stop-then-load', 'both'),
                    default='both')
    ap.add_argument('--hold-first', type=float, default=10.0,
                    help='Seconds to hold an initial load before swapping. '
                         '0 = swap from idle (synthetic). Default 10s = '
                         'matches live filter-change after wall warm-up.')
    args = ap.parse_args()

    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(WALL_DIR, 'config.ini'))
    c = {'url': cfg.get('Login', 'server_url').rstrip('/'),
         'user': cfg.get('Login', 'username'),
         'pw': cfg.get('Login', 'password')}
    s, tok, uid = auth(c)
    print(f'auth -> {c["url"]}')
    items = fetch_items(s, c['url'], uid, tok, n=200)
    print(f'items: {len(items)}')

    modes = ['replace', 'stop-then-load'] if args.mode == 'both' else [args.mode]

    for mode in modes:
        print('\n' + '=' * 90)
        print(f'MODE: {mode}  ({args.cells} cells x {args.cycles} cycles x {args.play_secs}s/cycle)')
        print('=' * 90)
        cells = [Cell(i + 1) for i in range(args.cells)]
        try:
            cycle_pass = []
            for cy in range(args.cycles):
                # First cycle pre-loads to put cells in mid-decode state;
                # subsequent cycles run back-to-back swaps to amplify any
                # state corruption.
                hf = args.hold_first if cy == 0 else 5.0
                results = run_cycle(cells, items, c['url'], tok, args.play_secs, mode, hold_first=hf)
                passed = sum(1 for r in results if r[4])
                cycle_pass.append(passed)
                print(f'  cycle {cy + 1}: {passed}/{len(results)} cells played >={args.play_secs * 0.5:.1f}s')
                for idx, ff, pos, eof, ok, name in results:
                    if not ok:
                        ff_s = f'{ff:.2f}' if ff else '  -  '
                        print(f'    FAIL cell {idx}: ff={ff_s} pos={pos:.1f} eof={eof}  {name}')
            total = sum(cycle_pass)
            outof = len(cells) * args.cycles
            print(f'  TOTAL [{mode}]: {total}/{outof} ({total / outof * 100:.0f}%)')
        finally:
            for cell in cells:
                cell.stop()


if __name__ == '__main__':
    main()
