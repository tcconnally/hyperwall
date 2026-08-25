# Hyperwall v10

Fullscreen multi-monitor video wall for Emby media servers. Select displays
and libraries in a wizard, and Hyperwall fills each monitor with a grid of
hardware-accelerated video cells powered by libmpv.

## Features

- **Multi-monitor** — each monitor gets its own fullscreen window with a
  configurable grid of video cells (1x1 to 6x6)
- **Per-monitor layout** — the setup wizard independently assigns each
  selected display a Wall/Preview role, physical rotation (Auto/0°/90°/180°/
  270°), and rows × columns video grid
- **libmpv backend** — hardware-accelerated decode via nvdec/d3d11
  (NVIDIA Blackwell), 240 Hz G-Sync compatible, HDR hinting
- **Emby integration** — streams directly from your Emby server with
  auto-transcode for 4K sources, favorites filtering, and per-cell
  tag/favorite controls (Jellyfin support is experimental — see Configuration)
- **Web remote** — built-in dark-mode control page on port 8585
  (phone/tablet — no app install needed)
- **G-Sync isolation** — per-app NVIDIA Profile Inspector profile
  disables VRR for Hyperwall only, avoiding mixed-FPS jitter

## Quick Start (macOS — Apple Silicon / Intel, experimental)

macOS support uses a different video path: mpv's Swift backend does **not**
support `--wid` window embedding, so each cell renders through the libmpv
render API (`vo=libmpv`) into a QOpenGLWidget — the same architecture as
IINA/IPTVnator — with VideoToolbox hardware decode.

```
# 1. Clone
git clone https://github.com/tcconnally/hyperwall.git
cd hyperwall

# 2. Bootstrap (brew install mpv, creates .venv, installs deps, verifies libmpv)
./bootstrap.sh

# 3. Configure
cp config.example.ini config.ini   # bootstrap does this if missing
open -e config.ini                 # fill in server_url, username, password

# 4. Run
./launch.sh
```

macOS notes:

- Requires Homebrew; `brew install mpv` provides `libmpv.dylib`.
  `launch.sh` exports `DYLD_FALLBACK_LIBRARY_PATH` so python-mpv finds it
  (must be set before Python starts — don't skip launch.sh).
- Multi-monitor fullscreen works best with *System Settings → Desktop &
  Dock → Displays have separate Spaces* enabled (default).
- G-Sync isolation and the .exe build are Windows-only; macOS runs script
  mode with CoreAudio + VideoToolbox.
- If cells show software decode or black frames, try
  `HYPERWALL_HWDEC=videotoolbox-copy ./launch.sh`.

## Quick Start (Windows)

```powershell
# 1. Clone
git clone https://github.com/tcconnally/hyperwall.git
cd hyperwall

# 2. Bootstrap (installs deps, downloads mpv-2.dll, builds exe)
pwsh -ExecutionPolicy Bypass -File .\bootstrap.ps1

# 3. Configure
Copy-Item config.example.ini config.ini
notepad config.ini    # fill in server_url, username, password

# 4. Run
.\launch.bat
# or: python hyperwall.py
# or: .\hyperwall.exe  (recommended — enables G-Sync isolation)
```

## Requirements

- Windows 10/11 with PowerShell 7+ — or — macOS (Apple Silicon/Intel) with
  Homebrew (experimental)
- Python 3.12+
- NVIDIA GPU with driver 551+ (Windows nvdec hardware decode); Apple
  Silicon uses VideoToolbox
- Emby server on local network
- NVIDIA Profile Inspector (optional — for G-Sync isolation, Windows only)

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `C` | Toggle controls visibility |
| `Space` | Global pause/resume |
| `F` | Favorites filter |
| `A` | All-items filter |
| `S` | mpv stats overlay |
| `Esc` | Shutdown |

## Web Remote API

All endpoints under `/api/`:

| Endpoint | Method | Description |
|---|---|---|
| `/api/status` | GET | Full wall state |
| `/api/pause` | POST | Toggle global pause |
| `/api/next/<n>` | POST | Next video on cell n |
| `/api/prev/<n>` | POST | Previous video on cell n |
| `/api/loop/<n>` | POST | Toggle loop on cell n |
| `/api/mute/<n>` | POST | Toggle mute on cell n |
| `/api/filter` | POST | Set filter (all/favorites) |
| `/api/controls` | POST | Toggle controls |
| `/api/shutdown` | POST | Shut down wall |

## Architecture

```
hyperwall.py → hyperwall/app.py → WallController
                                    ├── SetupWizard (per-monitor role/rotation/grid + library picker)
                                    ├── Per-monitor QMainWindow (fullscreen)
                                    │   └── Grid of VideoCell widgets
                                    │       └── mpv.MPV embedded via wid=
                                    ├── ContentLoader → Emby REST API
                                    ├── web.py (Flask remote on :8585)
                                    └── nvidia.py (G-Sync per-app disable)
```

## Configuration

`config.ini` (copied from `config.example.ini`):

```ini
[Login]
server_url = http://192.168.1.100:8096
username = your_username
password = your_password

[Settings]
# These are fallback defaults; the wizard stores per-monitor overrides.
last_grid_rows = 2
last_grid_cols = 2
last_display_roles =
last_display_layouts =
cleanup_on_startup = false
```

Environment variables:

| Variable | Effect |
|---|---|
| `HYPERWALL_WEB=1` | Enable web remote on port 8585 (off by default) |
| `HYPERWALL_SERVER_URL` | Per-launch Emby endpoint override; leaves `config.ini` unchanged (use for LAN/public A/B) |
| `HYPERWALL_STATS=1` | Enable per-cell playback stats |
| `HYPERWALL_HWDEC` | Override hardware decoder (nvdec, d3d11va, etc.) |
| `HYPERWALL_VO` | Override video output (gpu-next, gpu) |
| `HYPERWALL_NO_RELAUNCH=1` | Skip exe re-launch (script mode) |
| `HYPERWALL_ISOLATED=1` | Force G-Sync isolation on (bypass exe-name check) |
| `HYPERWALL_AUTO_TRANSCODE=0` | Disable auto-transcode heuristic |
| `HYPERWALL_STABLE_DIRECT_ONLY` | Force (`1`) or disable (`0`) the fail-closed direct-only pool. Auto-enabled only for an 8-cell macOS host with <=20 GiB RAM. |
| `HYPERWALL_STABLE_MAX_FPS` | Stable-pool frame-rate ceiling (default 30 fps) |
| `HYPERWALL_STABLE_MAX_BITRATE_MBPS` | Stable-pool bitrate ceiling (default 20 Mbps) |
| `HYPERWALL_STALL_TIMEOUT_S` | Stall watchdog: flag a frozen stream after N s of no progress (default 20) |
| `HYPERWALL_WATCHDOG_MS` | Stall watchdog poll interval in ms (default 5000) |
| `HYPERWALL_CRASHLOOP_THRESHOLD` | Failures within the window before a cell is parked (default 5) |
| `HYPERWALL_CRASHLOOP_WINDOW_S` | Rolling window for the crash-loop guard (default 60) |
| `HYPERWALL_CRASHLOOP_COOLDOWN_S` | How long a parked cell waits before resuming (default 120) |
| `HYPERWALL_CACHE_BUDGET_MB` | Aggregate demuxer cache ceiling across all cells (default 8192 MiB on Windows/Linux; 2048 MiB on macOS hosts up to 20 GiB RAM) |
| `HYPERWALL_DEMUXER_PER_CELL_MB` | Desired per-cell demuxer cache before budget scaling (default 1024 MiB on Windows/Linux; 256 MiB on small macOS hosts) |
| `HYPERWALL_PERFTRACE=1` | Emit GUI loop-lag and slow-slot telemetry |
| `HYPERWALL_SOAK_MINUTES` | Run a self-terminating randomized soak for N minutes |
| `HYPERWALL_SOAK_PROFILE` | Soak mix: `mixed` (default), `audio` (mute/unmute focus), or `advance` |
| `HYPERWALL_SOAK_REPORT_DIR` | Write JSONL run events (start/sample/finish) to this directory |

### macOS playback soak

For the reported mute/unmute jank, use the audio-focused launcher on the M5:

```bash
chmod +x soak_wall.sh
./soak_wall.sh 60
```

It runs a 60-minute self-terminating wall session, keeps at most one cell
unmuted, and biases actions toward lazy-audio arm/relock transitions. Each run
creates `soak_reports/<timestamp>/` with `hyperwall.log`, JSONL events,
`vm_stat.log`, `nettop.log`, and (where permitted) `powermetrics.log`. The
final `hyperwall_stats_*.json` records VideoToolbox/decode/drop/freeze totals.
To test a different hardware-decoder path, run a separate, otherwise identical
session, e.g. `HYPERWALL_HWDEC=videotoolbox-copy ./soak_wall.sh 60`; do not mix
profiles in one run.

### One-command no-image diagnostics

Run the repository checks, an unauthenticated source-health probe, and a
short live phase with offline parsing in one command. Declare the expected
total cell count so a saved grid cannot be mislabeled:

```bash
python3 scripts/run-soak-diagnostics.py --minutes 10 --expected-cells 8 --decoders no
```

The runner writes a timestamped directory under `soak_reports/`, redacts text
copies for sharing, and returns nonzero when a measured reliability or
cell-count gate is blocked. It collects application logs, JSONL soak events,
final per-cell stats, `vm_stat`, `nettop`, and best-effort `powermetrics`; it
captures **no images, screenshots, or video**. Use `--decoders no` to isolate
server auto-transcoding from the Mac decoder, or select another decoder for a
separate decoder experiment. `--expected-cells 4` or `--expected-cells 8`
blocks a phase if the final stats contain a different number of cells. A
source-health failure is reported separately from a client/decoder failure.
The current checkout still requires one manual SetupWizard acceptance per
live phase; the runner prints this notice rather than automating GUI clicks.

The default 10-minute phases are a pilot. Run the full soak only after the
pilot is clean:

```bash
python3 scripts/run-soak-diagnostics.py --minutes 60 --expected-cells 8 --decoders no
```

Do not share the raw phase directories: use the `*-redacted/` copies because
raw media URLs may contain credentials or session identifiers.

## Building

```cmd
pip install pyinstaller
build.bat
```

Produces `hyperwall.exe` — a versionless basename. G-Sync isolation is gated
on the `hyperwall*.exe` prefix (or `HYPERWALL_ISOLATED=1`), so the exe name is
stable across version bumps and the NVIDIA profile keeps matching.

## Testing

The test suites are pure-logic and dependency-light — no PyQt/mpv/Emby, no
pytest. They run headless in CI (`.github/workflows/repo-guards.yml`) and
locally:

```bash
python tests/run_all.py
```

| Suite | Covers |
|---|---|
| `run_repo_guards` | Package structure + version-drift guard (no `hyperwall_v<N>` / hardcoded version literals) |
| `test_reliability` | Stall watchdog, crash-loop guard, cache-budget scaling, retry→transcode→skip escalation |
| `test_urls` | Emby URL construction (incl. load-bearing `static=true`) + transcode heuristic boundaries |
| `test_config` | `config.ini` save/load round-trip, typed fields, frozen dataclass, scene presets |
| `test_playlist` | Multi-source playout: per-group de-dup, refill/reshuffle, group independence |
| `test_scenes` | Scene-preset serialization round-trip + malformed-input safety |
| `test_backends` | Emby/Jellyfin backend specs: Emby parity, Jellyfin auth-header + verified-live gate |

## License

MIT
