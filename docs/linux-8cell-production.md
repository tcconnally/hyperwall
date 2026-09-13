# Pop!_OS / NVIDIA 8-cell production path

Status: **implementation ready; live GPU/display qualification pending**

This is the closed-environment production path for the Pop!_OS host with an
RTX 5070 Ti. It is intentionally not a cross-platform compatibility profile.
The wall uses Qt's libmpv render API instead of native child-window embedding,
so the client does not depend on X11 `wid` embedding when Pop!_OS is running
Wayland.

## Runtime contract

1. Convert every current library item and every new item into the wall-safe
   library before Emby indexes it.
2. The normalized output is MP4, H.264 video, AAC audio when audio is present,
   at most 1920×1080, 30 fps, two audio channels, and a conservative 10 Mbps
   video ceiling.
3. Point Emby at the normalized output root. Do not mix original and output
   roots in the production wall library.
4. Launch with `launch-linux.sh`. It enables
   `HYPERWALL_NORMALIZED_LIBRARY=1`, disables runtime HLS transcoding, and
   requires exactly one visible RTX 5070 Ti with at least 12 GiB reported VRAM.
   It then unloads resident Ollama models and verifies `/api/ps` is empty before
   starting the wall.
5. If an item is missing the normalized metadata contract, Hyperwall excludes
   it and logs the count; it does not silently send that item into a live HLS
   transcode path.

The source library is never deleted by the normalizer. The destination is a
separate tree and each output is written to a suffix-preserving temporary MP4
before atomic publication.

## Initial conversion

Dry-run first:

```bash
python3 scripts/normalize-library.py \
  --source /srv/media-original \
  --destination /srv/media-wall-safe
```

Convert and verify:

```bash
python3 scripts/normalize-library.py \
  --source /srv/media-original \
  --destination /srv/media-wall-safe \
  --execute --strict
```

Only after the command reports `status: "ok"` should `/srv/media-wall-safe`
be added to an Emby library. Existing outputs are re-probed and skipped when
already valid; failed or incomplete outputs are rebuilt without touching the
source.

For new media, add it to the original ingest root, rerun the normalizer, and
wait for the verified output before exposing it to Emby. The wall is not an
arbitrary-library player in this mode; the normalized library is the product
boundary that makes eight-cell playback bounded.

## Why this avoids the known failure

The prior 8-cell M5 runs showed clean Greg transport but media failures in the
client: malformed/software decode errors, freezes, A/V desynchronization,
audio underruns, loop stalls, and very high frame drops. The rejected runtime
30-fps/25-Mbps live-transcode experiment instead produced HLS 404/500 failures
and forced heavy items back to direct play. Raising Emby resources may help
other workloads, but it does not solve either failure mode reliably. Offline
conversion removes that per-cell runtime transcode dependency.

## Qualification gate

This branch is not a measured 8-cell pass until the target host produces fresh
evidence. The first Pop!_OS gate must record:

- exact Hyperwall commit and launch environment;
- NVIDIA driver, CUDA/NVDEC visibility, Qt, libmpv, and kernel versions;
- two physical displays and four cells per display;
- decoder backend per cell, frame callbacks, paints, renders, paint gaps,
  loop lag, frame drops, CPU, GPU residency, and thermal/power state;
- zero blocking freezes, decoder faults, audio underruns, transport errors,
  A/V desyncs, and loop stalls ≥100 ms during the short profile;
- 95% or better active-duration coverage.

Run 60–120 seconds first, then 30 minutes, then one physically awake
60-minute run. Missing host telemetry remains `BLOCK`; a clean process exit is
not a playback pass. Keep the existing M5 capacity selector fail-closed: its
open 4/6/8-cell evidence gate is not satisfied by static code inspection.

## KVM / HDMI disconnect benchmark

The physical-display gate and the decode/transport gate are intentionally
separate. `scripts/run-linux-disconnect-benchmark.py` runs one `mpv` worker per
cell with `--vo=null --ao=null`, polls `/sys/class/drm/*/status`, ignores
`SIGHUP`, samples `nvidia-smi`, and accounts for suspend gaps with
`CLOCK_BOOTTIME`. A KVM can remove every HDMI/DP connector without terminating
this measurement.

Run it only with normalized wall-safe media. Start with the KVM showing at least
one connected output; `--require-disconnect` now requires the benchmark to observe
an actual connected→all-disconnected transition, not merely begin disconnected:

```bash
python3 scripts/run-linux-disconnect-benchmark.py \\
  --input /srv/media-wall-safe/sample.mp4 \\
  --output /srv/hyperwall-reports/kvm-disconnect-$(date -u +%Y%m%dT%H%M%SZ) \\
  --cells 8 --duration-s 120 --poll-s 2 --require-disconnect --keep-awake
```

For a live handoff test, omit `--require-disconnect`, begin with the displays
connected, switch the KVM, and confirm an `all_disconnected` event followed by
continued cell lifetime. The report redacts exact source arguments and removes
raw temporary logs before it can return `completed`. `decode_transport_verdict`
and `headless_disconnect_resilience` can pass here, while `presentation_gate`
and the top-level `verdict` remain `BLOCK` by design until a physical scanout
run passes.

## Rollback / diagnostics

The hardware, media, and Ollama gates are fail-closed. For a controlled diagnostic
comparison only, either gate may be bypassed explicitly:

```bash
HYPERWALL_HARDWARE_PREFLIGHT=0 \
HYPERWALL_UNLOAD_OLLAMA=0 \
./launch-linux.sh
```

The launcher accepts only `0` or `1` for `HYPERWALL_NORMALIZED_LIBRARY` and
`HYPERWALL_AUTO_TRANSCODE`. It rejects the unsafe state where both are `0`;
that state would send unnormalized media to direct playback. Never use these
overrides for production or an overnight run.

For the separate media-policy rollback comparison:

```bash
HYPERWALL_NORMALIZED_LIBRARY=0 \
HYPERWALL_AUTO_TRANSCODE=1 \
./launch-linux.sh
```

That is not the production profile and should not be used for an overnight
wall until a separate gate proves the live-transcode path on this exact host.
