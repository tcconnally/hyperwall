# Pop!_OS / NVIDIA 8-cell production path

Status: **implementation ready; live GPU/display qualification pending**

This is the closed-environment production path for the Pop!_OS host with an
RTX 5070 Ti. It is intentionally not a cross-platform compatibility profile.
The wall uses Qt's libmpv render API instead of native child-window embedding,
so the client does not depend on X11 `wid` embedding when Pop!_OS is running
Wayland.

## Runtime contract

1. Keep the curated Emby library intact for qualification; do not replace it
   with a filtered wall-safe subset when measuring real-world playback.
2. The default launcher retains every item. Sources within the direct-play budget
   play directly; heavy or incomplete metadata is routed to bounded server
   H.264/AAC transcoding and remains observable in the logs.
3. A normalized output tree remains available as an explicit comparison corpus:
   MP4, H.264 video, AAC audio when audio is present, at most 1920×1080, 30 fps,
   two audio channels, and a conservative 10 Mbps video ceiling.
4. Launch with `launch-linux.sh`. It requires exactly one visible RTX 5070 Ti
   with at least 12 GiB reported VRAM, unloads resident Ollama models, and
   verifies `/api/ps` is empty before starting the wall.
5. Set `HYPERWALL_NORMALIZED_LIBRARY=1` only when intentionally measuring the
   normalized comparison corpus. That mode is not valid evidence for the
   complete curated-library workload because it excludes the remaining items.

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

Only after the command reports `status: "ok"` should the normalized tree be
used for the explicit comparison-corpus run. Existing outputs are re-probed
and skipped when already valid; failed or incomplete outputs are rebuilt without
touching the source. The normalizer is not a prerequisite for whole-library
qualification.

For new media, add it to the original ingest root and keep it in the curated
Emby library. The default wall will exercise it directly or through the bounded
server transcode path; the normalized tree can be used later for an isolated
comparison.

## Why this avoids the known failure

The prior 8-cell M5 runs showed clean Greg transport but media failures in the
client: malformed/software decode errors, freezes, A/V desynchronization,
audio underruns, loop stalls, and very high frame drops. The rejected runtime
30-fps/25-Mbps live-transcode experiment instead produced HLS 404/500 failures
and forced heavy items back to direct play. The default full-library profile is
now deliberately allowed to expose both classes of failure. The normalized tree
is retained as a separate comparison corpus, not as a substitute for that
coverage.

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

The repository wrapper reads the existing `config.ini`, selects from its
configured Emby library, and does not assume a local `/srv` media mount. It can
benchmark any curated item, including sources that fail the normalized contract;
those diagnostics are reported and do not block the run. Start with the KVM
showing at least one connected output; `--require-disconnect` requires an
observed connected→all-disconnected transition, not merely beginning
disconnected:

```bash
python3 scripts/run-linux-disconnect-benchmark-emby.py --list-items

ITEM_ID='<real Emby item ID>'
REPORT="$HOME/hyperwall-reports/kvm-disconnect-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$(dirname "$REPORT")"
python3 scripts/run-linux-disconnect-benchmark-emby.py \\
  --item-id "$ITEM_ID" \\
  --output "$REPORT" \\
  --cells 8 --duration-s 120 --poll-s 2 --keep-awake
```

Use `--list-wall-safe` only for an explicit normalized-corpus comparison; it is
not evidence for the complete curated-library workload.

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
`HYPERWALL_AUTO_TRANSCODE`. Full-library mode requires auto-transcode to remain
enabled; the explicit normalized-library mode is the direct-only comparison
profile. Never use the normalized mode as evidence that the complete curated
library is healthy.

For the explicit normalized comparison:

```bash
HYPERWALL_NORMALIZED_LIBRARY=1 \
HYPERWALL_AUTO_TRANSCODE=0 \
./launch-linux.sh
```

For normal whole-library qualification, leave both variables unset (the
launcher defaults them to `0` and `1`):

```bash
./launch-linux.sh
```
