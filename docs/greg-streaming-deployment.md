# Greg LAN playback preparation — 2026-09-27

Hyperwall retains Emby and its existing library item IDs. The NAS prepares
bounded playback copies once, then Emby serves the completed files directly.
The display layout, scaling, controls, favorites, and tags remain attached to
the original items.

## Deployed server configuration

- NAS: Greg, `10.168.168.29`, Intel i5-13500, Intel Arc A310, 2.5 Gb Ethernet.
- Client: Apple M5, 10 CPU cores (4 performance / 6 efficiency), 8 GPU cores,
  16 GB unified memory. Two LG UltraGear displays at 120 Hz: 2560×1440
  landscape and 1440×2560 portrait. Existing eight-cell layout is retained.
- Emby: existing LinuxServer container, 4 CPU limit and 6 GiB memory limit.
- Original media: `/mnt/user/greg/media/etc/mv`, exposed to Emby as
  `/media/etc/mv`.
- Dedicated Unraid share: `/mnt/user/hyperwall-media`, primary storage Array,
  high-water allocation, 50 GB minimum free; SMB and NFS export disabled.
- Portainer `media-servers` stack version 10 adds only the Emby bind mount
  `/mnt/user/hyperwall-media:/hyperwall`. No image was pulled. Plex and
  Jellyfin retained their existing containers.
- Official Folder Sync plugin 3.2.9, destination `Hyperwall 1080p`,
  `/hyperwall/mv`, root administrator access only.
- Conversion working directory: `/hyperwall/.work`. Full-speed conversion
  remains disabled.
- Initial bulk preparation jobs used custom MP4 / H.264 / AAC at 4 Mbps.
  The failure-only policy now cancels their unfinished work: 753 remaining
  queued/incomplete items were cancelled, plus the earlier active incomplete
  item. All 152 completed copies were verified retained. The conversion task
  is idle. Originals were never modified.
- Automatic future work is one individually identified problematic video per
  job, with `SyncNewContent=false`; no blanket library conversion remains.

The `mv` source inventory found 856 video files (606 GiB, 88.1 hours). Emby
exposes 851 logical items in that library; file count and logical-item count
are different measures.

## Client profile

Run `./run-hyperwall.sh` on macOS or Linux for full-library direct playback
with local telemetry. All 906 unique items became available in 0.201 seconds
in the latest read-only loader check. Background source enrichment retained
all 906 items and found 151 receipt-verified copies in 20.743 seconds.

Resolution, bitrate, codec metadata and missing prepared copies do not mark
an item problematic. `HYPERWALL_AUTO_TRANSCODE=0` disables speculative live
transcoding. The separate failure queue accepts attributed malformed-stream
errors, exhausted decoder recovery and repeated explicit playback failures
outside an outage. Transport errors, buffering, watchdog stalls and VO frame
drops do not qualify. A daemon records evidence under `logs/transcode-queue/`
and submits a single-video job to the existing `Hyperwall 1080p` destination.
Existing jobs are checked first; uncertain submissions are confirmed without
repeating the POST. Failed/unconfirmed requests remain visible locally.

Completed prepared sources are discovered by their explicit Emby
`MediaSourceId`; item identity, favorites and tags remain attached to the
original. A refresh every minute updates future selections and preserves
active playback, prefetch and the current shuffle cycle. Telemetry includes
the actual full and filtered pool sizes.

The optional prepared-only comparison remains available with
`HYPERWALL_PREPARED_ONLY=1`. Its initial discovery order is shuffled to avoid
always starting from the first 16 files in server order. It uses a 64 MiB
per-cell cache and 768 MiB aggregate ceiling. Full-library playback retains
the ordinary cell-count-aware cache budgets. M5 decoding uses
`videotoolbox-copy`; Linux uses `auto-safe` unless overridden.

The saved client endpoint is the NAS LAN address, `http://10.168.168.29:8096`.
Media delivery does not need the public reverse proxy.

Prepared audio remains selected across mute and volume changes, so these
controls do not select an audio track or seek the video. Render-API native
controls run outside Qt's rendering thread. Qt presentation timing is
reported to libmpv after the actual window swap.

## Finalization worker

The host worker runs the existing container's FFmpeg as UID/GID 99:100. It
waits for two unchanged observations at least 60 seconds apart, validates
H.264 8-bit video within 1080p/60 fps and AAC mono/stereo, then losslessly
remuxes with fast-start metadata and audio/video interleaving. It validates
the result before atomically replacing the derived copy and appending the
64-byte preparation receipt. Original media is never rewritten.

The worker is installed at
`/mnt/cache/appdata/hyperwall-prep/finalize-emby-renditions.py`. Its log and
PID are in `finalizer.log` and `finalizer.pid` in that same directory. It
reserves 50 GiB of free space and stops after 72 hours or 851 ready files.
The bulk queue is cancelled; this worker only finalizes existing derived
copies and does not start encoding originals. It remains a bounded job,
so finalization must be resumed after its deadline or a NAS restart if new
failure-triggered conversions need preparation receipts.
That file count is an exit condition; final library coverage must also be
checked against unique Emby item IDs. Incompatible files remain excluded.

To resume after a NAS restart, run on Greg:

```sh
nohup python3 /mnt/cache/appdata/hyperwall-prep/finalize-emby-renditions.py \
  --root /mnt/user/hyperwall-media/mv --container-root /hyperwall/mv \
  --work /mnt/user/hyperwall-media/.work/finalizer \
  --container-work /hyperwall/.work/finalizer --container emby \
  --watch-seconds 60 --max-hours 72 \
  >> /mnt/cache/appdata/hyperwall-prep/finalizer.log 2>&1 < /dev/null &
echo $! > /mnt/cache/appdata/hyperwall-prep/finalizer.pid
```

A filesystem lock prevents duplicate workers. The private ledger preserves
failure reasons; unchanged failed inputs are not retried automatically.
The worker is a bounded one-time preparation job, not a permanent service.

## Measurements and limits

- Full-library admission now returns all 906 unique items in 0.201 seconds.
  Source enrichment takes 20.743 seconds in the background and retains all
  items. Native sync readback confirms the conversion task is idle and
  completed copies remain available; no synthetic failing file was submitted
  to the real conversion queue to test job creation.
- Actual user-started eight-cell sessions show zero decoder drops and zero
  cache-freeze duration in the sampled records, but substantial VO frame
  drops remain. These are presentation symptoms under investigation, not
  sufficient evidence to queue those files for conversion. Short null-output
  decoder tests do not establish smooth visible rendering.

- Twelve distinct original static stream endpoints returned HTTP 206 with
  correct byte ranges: 96 MiB in 0.856 seconds, about 940.55 Mbps aggregate.
  This is a bounded burst-delivery test, not a sustained display benchmark.
- Pilot item 21878: original 4K MPEG-4 at 120 fps and about 82 Mbps;
  prepared H.264 1920×1080 at 60 fps with AAC stereo, about 4 Mbps.
  Intel Quick Sync encoded the pilot at approximately 1.54× real time.
- Twelve 4 Mbps streams need approximately 48 Mbps before protocol overhead.
- M5 native decoder comparison, ten settled seconds per player with null
  video/audio outputs, one prepared 1080p60 pilot at varied offsets:
  eight VideoToolbox-copy players peaked at 1,083 MiB RSS; twelve peaked at
  1,542.4 MiB. Twelve hardware players used 49.5% of one CPU core versus
  188.3% for software decoding, a 73.7% reduction. All twelve confirmed
  hardware decoding, realtime progress, zero decoder drops, buffering,
  backwards jumps, and native errors. These are decoder/transport results,
  not a Qt/OpenGL presentation or long-session benchmark.
- Native audio probe on the prepared pilot: 24 mute/volume changes over
  7.979 seconds; playback advanced 7.983 seconds with zero seeks, playback
  restarts, backwards jumps, or buffering events. Null audio/video outputs
  avoided a visible wall or audible playback.
- After finalization, twelve **distinct** receipt-verified files played
  concurrently with VideoToolbox-copy and 288 production mute/volume calls:
  zero seeks, restarts, buffering events, decoder drops, backward jumps or
  native errors. Peak RSS was 1,750.4 MiB and CPU was 52% of one core. This
  was another ten-second null-output probe, not an on-screen soak.
- Live source audit retained all 851 unique original items and their
  identity/favorite/tag metadata. At that checkpoint, 69 normalization
  receipts were verified and 75 alternate sources were available. Source
  resolution took 14.167 seconds on the background content loader.
- A user-run eight-cell session exposed a startup regression: all cells
  remained empty while a 906-item source audit ran for about 17 seconds.
  Progressive discovery supplies the first eight receipt-verified copies
  in 1.397 seconds. The final startup profile reserves one active and one
  prefetched copy per cell: 16 verified copies arrived in 2.280 seconds,
  with the full scan finishing later (22.283 seconds, 129 ready at this
  checkpoint). This prevents prefetch from exhausting the first shuffle
  cycle before all eight cells start. The initial eight files passed a native
  concurrent playback probe with 192 audio operations and zero buffering,
  decoder errors, seeks or restarts. This was a headless probe; visible
  presentation still requires a user-started run.
- First Escape now hides every wall/solo window before cleanup. A daemon
  forces process exit after a 200 ms grace period. The offscreen blocked-
  cleanup harness measured a 0.29 ms handler and 204.49 ms mocked exit;
  physical screen/audio latency and delivery through an already-wedged Qt
  event loop were not measured. Periodic local telemetry survives without
  relying on an exit-time stats flush.
- No graphical wall was automatically launched. Actual multi-display
  rendering and long-session behavior require a user-started run.

The final offscreen suite passed 606 checks across 43 suites, with platform
skips and no failures. Redacted measurements are under `docs/validation/`.

## Recovery

The pre-change and deployed stack files are preserved on the NAS under
`/mnt/cache/appdata/hyperwall-prep/media-servers.before.yml` and
`media-servers.after.yml`, mode 600. Portainer also retains stack version 9.
The original media files were not rewritten. To use the original client
behavior, run normal `launch.sh` without the prepared profile variables.

Do not delete the prepared share or cancel jobs with a delete-files option
as a routine rollback. Stop preparation first and retain the copies until
they are no longer needed.
