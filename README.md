# Hyperwall

Fullscreen multi-monitor video wall for Emby media servers. Hyperwall is
macOS-native: each cell renders through libmpv's render API into a Qt OpenGL
widget, with CoreAudio output and an explicit VideoToolbox/software decoder
profile.

## What the macOS path provides

- One fullscreen Qt window per selected display, with independent role,
  rotation, and 1x1 through 6x6 grid settings.
- Libmpv render callbacks and a coalescing GUI frame pump. Cells do not use
  native window-ID embedding.
- Emby playback, favorites filtering, tags, per-cell controls, and the optional
  web remote on port 8585.
- Bounded M-series cache budgets: 256 MiB per cell and a 2 GiB aggregate
  demuxer ceiling through 24 GiB of RAM.
- Dependency-light startup. Emby JSON transport uses Python's standard library;
  the runtime does not require `requests`.
- Opt-in soak telemetry for loop lag, render timing, decoder counters, memory,
  power, and network evidence.

## Quick start

Requirements:

- macOS on Apple Silicon
- Python 3.12 or newer
- Homebrew and `mpv` (`brew install mpv`)
- An Emby server reachable from the Mac

```bash
git clone https://github.com/tcconnally/hyperwall.git
cd hyperwall
./bootstrap.sh
cp config.example.ini config.ini
open -e config.ini
./launch.sh
```

`launch.sh` must be used for normal operation. It exports the dynamic-library
search path before Python starts and clears stale soak-only environment state.
The application rejects non-macOS hosts instead of entering an untested
platform path.

For multi-monitor fullscreen, enable **Displays have separate Spaces** in
System Settings if the wall should keep one fullscreen Space per display.

## M5 decoder profiles

The default `safe` profile uses software decode because the available M5
VideoToolbox soak evidence was not clean. RAM size does not silently select a
decoder. Hardware experiments are explicit and reversible:

```bash
# Safe measured baseline
HYPERWALL_M5_DECODER_PROFILE=safe ./launch.sh

# First hardware pilot
HYPERWALL_M5_DECODER_PROFILE=hardware-copy ./launch.sh

# Direct VideoToolbox pilot
HYPERWALL_M5_DECODER_PROFILE=hardware ./launch.sh
```

`HYPERWALL_HWDEC` is the per-run override. Supported values are `no`,
`videotoolbox-copy`, `videotoolbox`, `auto`, and `auto-safe`. Run one profile
per soak; do not mix decoder modes in a comparison. A target-Mac run is required
before calling a profile stable. Headless CI cannot certify VideoToolbox,
WindowServer, display timing, or Qt teardown.

The measured evidence and promotion gates are kept in
[`docs/performance-roadmap.md`](docs/performance-roadmap.md).

## Configuration

`config.ini` is copied from `config.example.ini`:

```ini
[Login]
server_url = http://your-emby-host:8096
username = your_username
password = your_password

[Settings]
last_grid_rows = 2
last_grid_cols = 2
last_display_roles =
last_display_layouts =
cleanup_on_startup = false
```

Useful environment variables:

| Variable | Effect |
|---|---|
| `HYPERWALL_WEB=1` | Enable the web remote on port 8585 |
| `HYPERWALL_SERVER_URL` | Override the Emby endpoint for one launch |
| `HYPERWALL_STATS=1` | Enable per-cell playback statistics |
| `HYPERWALL_M5_DECODER_PROFILE` | Select `safe`, `hardware-copy`, or `hardware` |
| `HYPERWALL_HWDEC` | Override the decoder for one run |
| `HYPERWALL_RENDER_PROFILE` | Select `hq` or `low-cost` rendering |
| `HYPERWALL_VIDEO_SYNC` | Override mpv video synchronization for a pilot |
| `HYPERWALL_AUDIO_BUFFER` | Override the CoreAudio buffer for a pilot |
| `HYPERWALL_AUTO_TRANSCODE=0` | Disable the bounded auto-transcode planner |
| `HYPERWALL_STABLE_DIRECT_ONLY=1` | Use the explicit fail-closed direct-only escape |
| `HYPERWALL_CACHE_BUDGET_MB` | Override the aggregate demuxer cache ceiling |
| `HYPERWALL_DEMUXER_PER_CELL_MB` | Override the per-cell cache target |
| `HYPERWALL_PERFTRACE=1` | Emit GUI loop-lag and slow-slot telemetry |

Soak launchers set `HYPERWALL_NO_CONFIG_SAVE=1` so temporary SetupWizard
choices cannot overwrite `config.ini`. Normal launches clear that marker and
persist accepted configuration normally.

## Keyboard shortcuts

| Key | Action |
|---|---|
| `C` | Toggle controls |
| `Space` | Pause/resume all cells |
| `F` | Favorites filter |
| `A` | All-items filter |
| `S` | Toggle mpv statistics |
| `Esc` | Shut down the wall |

## Architecture

```text
hyperwall.py
  └── hyperwall/app.py
      ├── macos_runtime.py       macOS gate and decoder profiles
      ├── constants.py            libmpv options and M5 cache policy
      ├── WallController           fullscreen display windows and lifecycle
      │   └── VideoCell             per-cell playback state
      │       ├── macembed.py      libmpv render API / Qt OpenGL ownership
      │       └── frame_pump.py    coalesced GUI-thread frame delivery
      ├── EmbyClient               stdlib JSON HTTP transport
      ├── soak.py                  macOS RSS/thread and playback telemetry
      └── web.py                   optional Flask remote
```

The GUI thread owns Qt and OpenGL resources. Background work may request a
frame or playback action, but it cannot call widget or render-context methods.
During shutdown, render contexts are released while the owning GUI objects are
still alive, before mpv cores terminate.

## Soak diagnostics

First run a short one-cell pilot after accepting the SetupWizard for the phase:

```bash
HYPERWALL_SOAK_ACTIVE=1 \
HYPERWALL_SOAK_FILTER=favorites \
HYPERWALL_M5_DECODER_PROFILE=safe \
python3 scripts/run-soak-diagnostics.py \
  --minutes 10 \
  --expected-cells 1 \
  --decoders no
```

Run a hardware comparison as a separate phase:

```bash
HYPERWALL_SOAK_ACTIVE=1 \
HYPERWALL_SOAK_FILTER=favorites \
python3 scripts/run-soak-diagnostics.py \
  --minutes 10 \
  --expected-cells 1 \
  --decoders videotoolbox-copy
```

The runner performs pure-logic checks, creates a timestamped directory under
`soak_reports/`, collects app and host telemetry, and captures no images. A
`BLOCK` verdict is not evidence that the configured Emby library is wrong; read
the phase manifest and app log to distinguish setup, source health, watchdog,
decoder, transport, and GUI lifecycle failures. Raw reports may contain media
URLs or session identifiers. Share only generated redacted copies.

For render-tier profiling:

```bash
HYPERWALL_RENDER_PROFILE=low-cost ./launch.sh
python3 scripts/profile-macos-render.py --matrix \
  profile-4.json profile-6.json profile-8.json
```

A low-cost profile changes only the scaling/deband settings. It does not change
decoder selection, cache policy, or transport behavior.

## Web remote

When enabled, the remote exposes endpoints under `/api/`:

| Endpoint | Method | Description |
|---|---|---|
| `/api/status` | GET | Wall state |
| `/api/pause` | POST | Toggle global pause |
| `/api/next/<n>` | POST | Advance cell `n` |
| `/api/prev/<n>` | POST | Rewind cell `n` |
| `/api/loop/<n>` | POST | Toggle loop |
| `/api/mute/<n>` | POST | Toggle mute |
| `/api/filter` | POST | Set `all` or `favorites` |
| `/api/shutdown` | POST | Shut down the wall |

## Testing

The repository test runner is dependency-light and does not need pytest:

```bash
python3 tests/run_all.py
python3 -m compileall -q hyperwall tests
```

Pure-logic tests can run on CI. Tests that require PyQt6 are skipped when Qt is
not installed; only a real MacBook run can validate VideoToolbox, WindowServer,
OpenGL ownership, multi-display behavior, or long-soak timing.

## License

MIT
