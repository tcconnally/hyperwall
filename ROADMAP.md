# Hyperwall macOS/M5 roadmap

This repository targets Apple Silicon macOS. The runtime path is libmpv's render
API in `MpvGLWidget`, CoreAudio, the coalescing frame pump, and bounded Emby
session/cache lifecycles. No alternate desktop renderer or executable wrapper is
part of the product path.

## Current architecture

- `macos_runtime.py` rejects non-macOS application starts and resolves an
  explicit decoder profile.
- `constants.py` owns the single libmpv option set and M5 Air cache budget.
- `cell.py` always uses `MpvGLWidget`; all load, prefetch, mute, and volume
  native calls are serialized or deferred away from the GUI handler.
- `wall.py` releases Qt/OpenGL render contexts on the GUI thread before mpv
  core termination.
- `http_client.py` provides the Emby JSON transport with Python stdlib only.
- `soak.py` records current RSS, peak RSS, threads, frame/render counters, and
  playback reliability events.

## Decoder policy

The default `safe` profile uses software decode because the available M5
VideoToolbox run was not clean. RAM size must not silently choose a decoder.
Hardware profiles remain explicit experiments:

| Profile | Decoder | Use |
|---|---|---|
| `safe` | `no` | measured baseline |
| `hardware-copy` | `videotoolbox-copy` | first VideoToolbox pilot |
| `hardware` | `videotoolbox` | direct VideoToolbox pilot |

A target Mac must pass a short pilot before a profile is used for a long soak.
The report must separate GUI stalls, cache starvation, decoder faults, transport
errors, audio underruns, and teardown failures.

## Performance stages

### Stage 0: target-host probe

Run a 60 to 120 second fixed-content profile at 1, 2, 4, 6, and 8 cells with
random churn disabled. Record exact HEAD, decoder state, callback/paint/render
counts, render duration, paint gaps, loop lag, process CPU, thermal state, GPU
residency, and decoder faults. Use `sample` and `powermetrics` when available.
Missing privileged telemetry is incomplete evidence, not a pass.

### Stage 1: frame-pump gate

Verify coalescing under callback bursts. The newest frame must win, queued GUI
notifications must remain bounded, and teardown must retain the C callback
trampoline until the render context is freed.

### Stage 2: render-quality gate

Compare `hq` and `low-cost` one variable at a time. The low-cost profile changes
scaling/debanding only. The pilot gate is p95 loop lag at or below 25 ms, no
100 ms loop stall, no freeze, no audio underrun, no A/V desync, no decoder fault,
and at least 95% active-duration coverage.

### Stage 3: decoder gate

Use hardware decode only when a known-good corpus passes on the target Mac.
Hardware faults trigger a bounded per-cell software fallback and resource
quarantine. One bad file must not change the wall-wide decoder policy.

### Stage 4: capacity gate

Measure 4, 6, and 8 cells with the same corpus and profile. Promote the highest
passing capacity. If 8 cells fail, keep the lower passing capacity as the M5
recommendation and document 8 cells as experimental. Missing evidence returns
`BLOCK` rather than selecting a default by guesswork.

### Stage 5: long validation

Only after the short and capacity gates pass, run one physically awake,
uninterrupted 30 to 60 minute validation with exact-HEAD, redacted artifacts.
The final gate requires no blocking freezes, decoder faults, audio underruns,
A/V desyncs, transport errors, or unexplained CPU escalation.

## Operator commands

Pure checks:

```bash
python3 tests/run_all.py
python3 -m compileall -q hyperwall tests
```

Render profiling:

```bash
HYPERWALL_RENDER_PROFILE=low-cost ./launch.sh
python3 scripts/profile-macos-render.py --matrix \
  profile-4.json profile-6.json profile-8.json
```

One-cell decoder pilot:

```bash
HYPERWALL_SOAK_ACTIVE=1 \
HYPERWALL_SOAK_FILTER=favorites \
python3 scripts/run-soak-diagnostics.py \
  --minutes 10 --expected-cells 1 --decoders no
```

Run each hardware decoder as a separate otherwise-identical phase. The live
phase still requires manual SetupWizard acceptance. Raw reports remain private;
share only redacted copies.

## Evidence rules

- A clean process exit does not prove smooth playback.
- A headless CI pass does not certify VideoToolbox, OpenGL, WindowServer, or
  multi-display timing.
- `ru_maxrss` is a high-water value on macOS; pair it with current RSS samples
  before classifying retained cache as a leak.
- Every final claim names the exact HEAD, cell count, decoder, duration, active
  coverage, and optional telemetry that was unavailable.
