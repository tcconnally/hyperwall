# macOS M5 Performance Roadmap

Status: **Stage 0 — measurement and render-pump isolation**

This roadmap records the performance work for Hyperwall’s macOS libmpv render
path. It is deliberately evidence-led: a successful process exit or a clean
harness does not mean playback is smooth.

## Current decision

Do **not** repeat the same 8-cell soak with a different decoder flag. Do not
make VideoToolbox hardware decoding a global default until per-resource
fallback and a known-good media corpus pass the gates below.

The next experiment is a bounded native profile and frame-pump test, not
another 30- or 60-minute soak.

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

`hyperwall/macembed.py` currently emits a queued Qt signal for every mpv update
callback. Add one pending-update gate per cell so callback bursts do not create
unbounded GUI work, while preserving newest-frame semantics and the callback
trampoline lifetime during teardown.

**Exit gate:** race-tested coalescing; bounded queue notifications; no lost
newest frame; no increase in maximum paint gap; no callback-lifetime crash.

### Stage 2 — measured render-cost profile

If Stage 0 identifies the render path as material, benchmark named macOS-only
profiles one variable at a time: deband, downscale filter, upscale filter, and
downscaling correction. Keep the current HQ profile unchanged by default until
quality and responsiveness are measured.

**Pilot gate:** no loop stall >=100 ms, p95 loop lag <=25 ms, no freeze, audio
underrun, A/V desync, or decoder fault, and at least 95% active-duration
coverage.

### Stage 3 — per-resource decoder policy

Use hardware decode only for validated resource/codec/container classes. On a
hardware failure, perform one bounded software fallback, then quarantine the
resource if recovery fails. Never turn a single bad file into a wall-wide
policy change.

**Exit gate:** known-good corpus has zero hardware faults; malformed resources
are isolated; final stats identify requested decoder, active decoder, and
fallback reason per cell.

### Stage 4 — measured capacity policy

Run the selected candidate on 4, 6, and 8 cells. Promote the highest cell count
that meets the pilot/30-minute gates. If 8 cells fail, make 6 (or 4) the M5
default and document 8 cells as experimental.

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
