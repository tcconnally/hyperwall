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

## Quick Start (Pop!_OS + NVIDIA, closed-environment production)

The Pop!_OS path uses the Qt/libmpv render API and is designed for the RTX
5070 Ti wall. It does **not** use native `--wid` embedding and does **not**
perform runtime HLS transcoding in production.

```bash
# 1. Bootstrap the Linux runtime in a user-local .venv.
./bootstrap-linux.sh

# 2. Normalize the complete source library into a separate tree.
python3 scripts/normalize-library.py \\
  --source /srv/media-original \\
  --destination /srv/media-wall-safe \\
  --execute --strict

# 3. Point Emby at /srv/media-wall-safe only.
# 4. Configure the saved Hyperwall layout as 2×2 on each display (8 total).
# 5. Launch the bounded direct-play profile. The launcher first requires
#    exactly one visible RTX 5070 Ti with at least 12 GiB reported VRAM, then
#    unloads resident Ollama models and verifies that /api/ps is empty.
./launch-linux.sh
```

The normalizer publishes MP4/H.264/AAC at ≤1080p30 and ≤10 Mbps, verifies
with ffprobe, and never deletes source files. `launch-linux.sh` enables
`HYPERWALL_NORMALIZED_LIBRARY=1`; items without that contract are excluded
rather than sent to live HLS transcoding. The launcher accepts only `0` or `1`
for the normalized-library and auto-transcode switches, and rejects the unsafe
combination where both are disabled. See
[`docs/linux-8cell-production.md`](docs/linux-8cell-production.md) for the
qualification and rollback gates. The Pop!_OS/8-cell path is implementation
ready but not declared measured-pass until fresh GPU/display soak evidence
exists on the exact host.

For an explicitly controlled diagnostic run only, `HYPERWALL_HARDWARE_PREFLIGHT=0`
skips the GPU check and `HYPERWALL_UNLOAD_OLLAMA=0` keeps Ollama resident. Do
not use either override for production or overnight playback.

### KVM / HDMI-disconnect benchmark

For a display-independent decode/transport check, use the benchmark runner
below. It launches one null-video/null-audio mpv process per cell, polls DRM
connector state, ignores `SIGHUP`, and records suspend-aware coverage. It can
therefore continue when a KVM switches away from every HDMI/DP output. Use only
wall-safe inputs; source arguments are replaced with opaque labels in reports.
Start with at least one connected output when using `--require-disconnect`; the
flag requires an observed connected→all-disconnected transition rather than a
pre-existing disconnected state.

```bash
MEDIA=/tmp/hyperwall-linux-disconnect-benchmark/media/wall-safe.mp4
REPORT_ROOT="${HOME}/hyperwall-reports"
REPORT="$REPORT_ROOT/kvm-disconnect-$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$REPORT_ROOT"
test -r "$MEDIA"

python3 scripts/run-linux-disconnect-benchmark.py \\
  --input "$MEDIA" \\
  --output "$REPORT" \\
  --cells 8 --duration-s 120 --poll-s 2 --require-disconnect --keep-awake
```

`decode_transport_verdict=PASS` proves only that the eight decode/transport
workers survived the observed display disconnect. `presentation_gate` remains
`BLOCK` until the separate physical two-display run passes its frame, A/V,
freeze, and coverage gates. To test a real KVM handoff, omit
`--require-disconnect`, start with displays connected, switch the KVM during the
run, and inspect `display_events` in `summary.json`.

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
| `HYPERWALL_RENDER_PROFILE` | macOS render tier: `hq` (default) or explicit `low-cost`; ignored on Windows/Linux |
| `HYPERWALL_VO` | Override video output (gpu-next, gpu) |
| `HYPERWALL_NO_RELAUNCH=1` | Skip exe re-launch (script mode) |
| `HYPERWALL_ISOLATED=1` | Force G-Sync isolation on (bypass exe-name check) |
| `HYPERWALL_AUTO_TRANSCODE=0` | Disable auto-transcode heuristic |
| `HYPERWALL_STABLE_DIRECT_ONLY` | Explicit emergency escape: force (`1`) or disable (`0`) the fail-closed direct-only pool. It is **not** auto-enabled; normal playback retains the full library and uses bounded server H.264/AAC transcoding for heavy or unmeasured sources. |
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

### macOS performance roadmap

The evidence-led M5 performance plan, capacity gates, and decoder/rendering
policy are tracked in [`docs/performance-roadmap.md`](docs/performance-roadmap.md).
Do not repeat a long 8-cell soak until the roadmap's short profiling and
frame-pump gates pass.

For a bounded native profile on macOS, capture the running Hyperwall PID from
its startup log, then collect native stacks and privileged task telemetry:

```bash
sample <HYPERWALL_PID> 10 3 -file /tmp/hyperwall-sample.txt
sudo powermetrics --samplers tasks,thermal,gpu_power -i 1000 -n 120 \
  > /tmp/hyperwall-powermetrics.txt
python3 scripts/profile-macos-render.py \
  --powermetrics /tmp/hyperwall-powermetrics.txt \
  --sample /tmp/hyperwall-sample.txt \
  --pid <HYPERWALL_PID> \
  --output /tmp/hyperwall-profile.json
```

The profiler exits nonzero when either capture is missing or incomplete. A
permission-denied `powermetrics` file is incomplete evidence, not a pass.
To try the opt-in render tier, launch the same fixed corpus with:

```bash
HYPERWALL_RENDER_PROFILE=low-cost ./launch.sh
```

After converting each bounded run to a normalized profile or `analyze_run()`
JSON report, select the highest passing mode without guessing:

```bash
python3 scripts/profile-macos-render.py --matrix \\
  profile-4.json profile-6.json profile-8.json
```

The command returns `BLOCK` when required evidence is missing or no mode passes.
Required capacity evidence includes a `frame-drop-count` for every cell;
missing telemetry or any nonzero per-cell/aggregate drop count blocks mode
promotion.

### macOS playback soak

For the reported mute/unmute jank, use the audio-focused launcher on the M5:

```bash
chmod +x soak_wall.sh
./soak_wall.sh 60
```

The audio-enabled phase is paired with an audio-disabled control by preserving
all launch, media, grid, and power settings while disabling the action driver:

```bash
HYPERWALL_SOAK_ACTIONS=0 HYPERWALL_SOAK_PROFILE=advance ./soak_wall.sh 10
```

It runs a 60-minute self-terminating wall session, keeps at most one cell
unmuted, and biases actions toward lazy-audio arm/relock transitions. Each run
creates `soak_reports/<timestamp>/` with `hyperwall.log`, JSONL events,
`vm_stat.log`, `nettop.log`, `power_sleep.log`, and (where permitted) `powermetrics.log`. The
final `hyperwall_stats_*.json` records VideoToolbox/decode/drop/freeze totals,
render callback/paint/render timing, and the final per-cell audio state. Each
JSONL sample also carries bounded interval render counters and native drop
counter deltas, so startup/transition drops can be separated from steady-state
pressure. To test a different hardware-decoder path, run a separate,
otherwise identical session, e.g. `HYPERWALL_HWDEC=videotoolbox-copy
./soak_wall.sh 60`; do not mix profiles in one run.

### One-command no-image diagnostics

Run the repository checks, an unauthenticated source-health probe, and a
short live phase with offline parsing in one command. Declare the expected
total cell count so a saved grid cannot be mislabeled:

```bash
python3 scripts/run-soak-diagnostics.py --minutes 10 --expected-cells 8 --decoders no
```

The runner writes a timestamped directory under `soak_reports/`, redacts text
copies for sharing, and returns nonzero when a measured blocking reliability,
cell-count, or completeness gate fails. Non-blocking resource warnings remain
reported as `WARNING` in the summary instead of being promoted to `BLOCK`. It
collects application logs, JSONL soak events, final per-cell stats, `vm_stat`,
`nettop`, and best-effort `powermetrics`; it
captures **no images, screenshots, or video**. Use `--decoders no` to isolate
server auto-transcoding from the Mac decoder, or select another decoder for a
separate decoder experiment. `--expected-cells 4` or `--expected-cells 8`
blocks a phase if the final stats contain a different number of cells. A
source-health failure is reported separately from a client/decoder failure. To
run a controlled known-good or malformed-resource decoder phase, pass the exact
Emby item ID with `--item-id <id>`. The selector takes precedence over the
broad pool filter, requires exactly one matching item, records the ID in the
private phase manifest, and fails closed on a missing or duplicate match. The
current checkout still requires one manual SetupWizard acceptance per
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
