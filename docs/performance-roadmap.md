# macOS M5 Performance Roadmap

Status: **Implementation tranche merged; M5 runtime qualification pending**

This roadmap records the performance work for Hyperwall’s macOS libmpv render
path. It is deliberately evidence-led: a successful process exit or a clean
harness does not mean playback is smooth.

## Current decision

Do **not** repeat the same 8-cell soak with a different decoder flag. Do not
make VideoToolbox hardware decoding a global default until per-resource
fallback and a known-good media corpus pass the gates below.

The next experiment is a bounded native profile and frame-pump test, not
another 30- or 60-minute soak.

## What is usable now

- The macOS frame-update path coalesces callbacks before they enter the Qt
  event queue. Per-cell callback, queue, paint, and render counters are
  exported in final stats.
- `HYPERWALL_RENDER_PROFILE=low-cost` is an explicit macOS-only render tier.
  It uses bilinear scaling and disables debanding. The default HQ filters stay
  unchanged.
- Decoder telemetry records the requested and active decoder, hardware attempts
  and activations, software fallbacks, exhausted recovery, and quarantine.
- `scripts/run-soak-diagnostics.py --item-id <id>` selects one exact Emby item
  for a controlled decoder phase. It fails closed when the source response has
  zero or multiple matches and records the selector in the private manifest.
- `scripts/profile-macos-render.py` parses native captures. Its `--matrix`
  mode accepts normalized profile JSON or `analyze_run()` reports and selects
  the highest passing 4/6/8-cell mode. Missing evidence blocks selection.

`power_sleep_evidence` must be `1` only when the analysis report's
`power_sleep_evidence` gate is `PASS`; missing, warning, or blocked AC/lid/sleep
coverage is not promotable. A normalized matrix profile must include
`cell_count`, `duration_coverage`, `p95_loop_lag_ms`, `max_render_gap_ms`,
`cpu_cores_mean`, `loop_stalls_ge_100ms`, `freeze_count`, `decoder_faults`,
`audio_underruns`, `av_desync`, `transport_errors`, and
`power_sleep_evidence`. For example:

```bash
python3 scripts/profile-macos-render.py --matrix \\
  profile-4.json profile-6.json profile-8.json
```

## Evidence ledger

| Run | Configuration | Measured result | Decision |
|---|---|---|---|
| `20260828_161931` | 8 cells, direct-only, `hwdec=no`, 256 MiB/cell, 2 GiB aggregate | ~3,000 active seconds; 1,191 main-loop stalls; 32 cache-starvation freezes (~199.7 s); 206,131 VO drops; no decoder-frame drops | Software decode/render path is overloaded at this workload |
| `20260828_172945` | 8 cells, direct-only, `videotoolbox-copy`, 10 minutes | 28 loop stalls; 3 freezes (~23.9 s); 12,282 VO drops; 6 hardware failures; 4 fallback recoveries; 2 recovery exhaustions; 3 audio underruns; zero A/V desync warnings | Hardware decode is not safe as a global requirement |
| `hyperwall-powermetrics` | M5 task trace for the hardware trial | Hyperwall `Python` process averaged 2.14 CPU cores, p95 2.87, peak 3.41; Heavy thermal pressure in 542/600 samples; GPU active residency ~91.9% | CPU/native/render pressure is real; task names alone do not identify the hot stack |

### Corrected render comparison

The earlier comparison used the wrong denominator for the 50-minute run. With
elapsed time normalized correctly:

- software render time: **0.8275 core-equivalents**;
- hardware render time: **0.8313 core-equivalents**;
- render time per call: 6.602 ms software versus 4.227 ms hardware.

Hardware did not establish a total render-CPU win. The two runs also used
different media sequences, so their drop and stall rates are directional rather
than a controlled decoder A/B.

## Roadmap stages

### Stage 0 — minimal reproduction and native profile

Build a 60–120 second profile at 1, 2, 4, 6, and 8 cells with randomized
churn disabled. Record the exact commit, PID, decoder per cell, callback count,
coalesced callbacks, queued notifications, paints, renders, render duration,
paint gaps, loop lag, process CPU, thermal state, GPU residency, and decoder
faults. Use macOS `sample` plus sudo `powermetrics` when available.

**Exit gate:** the profile identifies whether the dominant time is Python
thread dispatch, native libmpv/FFmpeg decode, `mpv_render_context.render`, Qt
signal delivery, WindowServer, or driver/thermal work. Missing `powermetrics`
is incomplete evidence, not a pass.

### Stage 1 — coalesce the frame pump

`hyperwall/macembed.py` now emits at most one queued GUI update per cell while
an update is pending. The gate preserves newest-frame semantics and retains the
callback trampoline through teardown.

**Exit gate:** race-tested coalescing; bounded queue notifications; no lost
newest frame; no increase in maximum paint gap; no callback-lifetime crash.

### Stage 2 — measured render-cost profile

The opt-in `low-cost` macOS render profile is available for this comparison.
The default HQ profile remains unchanged. Benchmark the named profile one
variable at a time: deband, downscale filter, upscale filter, and downscaling
correction. Keep the current HQ profile unchanged by default until quality and
responsiveness are measured.

**Pilot gate:** no loop stall >=100 ms, p95 loop lag <=25 ms, no freeze, audio
underrun, A/V desync, or decoder fault, and at least 95% active-duration
coverage.

### Stage 3 — per-resource decoder policy

Use hardware decode only for validated resource/codec/container classes. The
runtime records hardware attempts, activations, software fallback, recovery
exhaustion, and quarantine per resource. On a hardware failure, perform one
bounded software fallback, then quarantine the resource if recovery fails.
Never turn a single bad file into a wall-wide policy change.

**Exit gate:** known-good corpus has zero hardware faults; malformed resources
are isolated; final stats identify requested decoder, active decoder, and
fallback reason per cell.

### Stage 4 — measured capacity policy

Run the selected candidate on 4, 6, and 8 cells. The capacity evaluator selects
the highest measured passing mode from those reports. If 8 cells fail, make 6
(or 4) the M5 default and document 8 cells as experimental. If all reports
fail or any required evidence is missing, it returns `BLOCK` with no default
selection.

Do not use the rejected runtime `30/25` shaping (`30 fps / 25 Mbps`) with live
transcoding. Use fewer cells or a pre-normalized wall-safe corpus instead.

### Stage 5 — long validation

Only after the short and 30-minute gates pass, run one physically-awake,
uninterrupted 60-minute validation with exact-head, redacted artifacts.

**Final gate:** >=95% active-duration coverage; zero blocking freezes, decoder
backend faults, audio underruns, A/V desyncs, and transport errors; no loop
stall >=100 ms; no unexplained CPU escalation.

## Work items

The implementation and discussion are tracked in the following GitHub issues:

- [#75: Frame-pump coalescing and callback safety](https://github.com/tcconnally/hyperwall/issues/75)
- [#76: Native stack profile and cell-scaling benchmark](https://github.com/tcconnally/hyperwall/issues/76)
- [#77: macOS low-cost render profile](https://github.com/tcconnally/hyperwall/issues/77)
- [#78: Per-resource VideoToolbox policy](https://github.com/tcconnally/hyperwall/issues/78)
- [#79: M5 capacity policy and promotion gate](https://github.com/tcconnally/hyperwall/issues/79)

## Safety rules for evidence

- Keep cache and audio changes out of the next primary experiment; audio-arm
  latency was single-digit milliseconds and muted intervals were not materially
  cheaper.
- Keep raw reports private. Share only redacted artifacts because media URLs
  can contain credentials or session identifiers.
- Every manual run must record exact commit, cell count, decoder state, active
  coverage, process PID, and whether sudo profiling actually ran.
