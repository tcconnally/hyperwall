# Hyperwall v10

Fullscreen multi-monitor video wall for Emby media servers. Select displays
and libraries in a wizard, and Hyperwall fills each monitor with a grid of
hardware-accelerated video cells powered by libmpv.

## Features

- **Multi-monitor** — each monitor gets its own fullscreen window with a
  configurable grid of video cells (1x1 to 6x6)
- **libmpv backend** — hardware-accelerated decode via nvdec/d3d11
  (NVIDIA Blackwell), 240 Hz G-Sync compatible, HDR hinting
- **Emby integration** — streams directly from your Emby server with
  auto-transcode for 4K sources, favorites filtering, and per-cell
  tag/favorite controls
- **Web remote** — built-in dark-mode control page on port 8585
  (phone/tablet — no app install needed)
- **G-Sync isolation** — per-app NVIDIA Profile Inspector profile
  disables VRR for Hyperwall only, avoiding mixed-FPS jitter

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

- Windows 10/11 with PowerShell 7+
- Python 3.12+
- NVIDIA GPU with driver 551+ (for nvdec hardware decode)
- Emby server on local network
- NVIDIA Profile Inspector (optional — for G-Sync isolation)

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
                                    ├── SetupWizard (monitor + library picker)
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
cleanup_on_startup = false
```

Environment variables:

| Variable | Effect |
|---|---|
| `HYPERWALL_WEB=1` | Enable web remote on port 8585 (off by default) |
| `HYPERWALL_STATS=1` | Enable per-cell playback stats |
| `HYPERWALL_HWDEC` | Override hardware decoder (nvdec, d3d11va, etc.) |
| `HYPERWALL_VO` | Override video output (gpu-next, gpu) |
| `HYPERWALL_NO_RELAUNCH=1` | Skip exe re-launch (script mode) |
| `HYPERWALL_ISOLATED=1` | Force G-Sync isolation on (bypass exe-name check) |
| `HYPERWALL_AUTO_TRANSCODE=0` | Disable auto-transcode heuristic |
| `HYPERWALL_STALL_TIMEOUT_S` | Stall watchdog: flag a frozen stream after N s of no progress (default 20) |
| `HYPERWALL_WATCHDOG_MS` | Stall watchdog poll interval in ms (default 5000) |
| `HYPERWALL_CRASHLOOP_THRESHOLD` | Failures within the window before a cell is parked (default 5) |
| `HYPERWALL_CRASHLOOP_WINDOW_S` | Rolling window for the crash-loop guard (default 60) |
| `HYPERWALL_CRASHLOOP_COOLDOWN_S` | How long a parked cell waits before resuming (default 120) |
| `HYPERWALL_CACHE_BUDGET_MB` | Aggregate demuxer cache ceiling across all cells (default 3072) |
| `HYPERWALL_DEMUXER_PER_CELL_MB` | Desired per-cell demuxer cache before budget scaling (default 512) |

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
| `test_config` | `config.ini` save/load round-trip, typed fields, frozen dataclass |

## License

MIT
