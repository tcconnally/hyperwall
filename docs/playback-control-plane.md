# Playback control plane

## Purpose

Hyperwall playback is a two-sided pipeline: Emby may produce either a direct
file or a server-transcoded HLS stream, while mpv independently chooses a
client decoder. These decisions are related by resource pressure, but they
are not the same decision. The control plane makes that distinction explicit
without replacing Qt, libmpv, or the native render surface.

## Ownership boundaries

```text
WallController
  ├── PlaybackPolicy / PlaybackPlan       pure server/client decision
  ├── ResourceGovernor                    transcode lease admission
  ├── EmbySessionBroker                   session records and stop cleanup
  └── VideoCell
        ├── CellPlaybackController        lifecycle and stale identity
        └── existing libmpv + Qt surface  native side effects
```

### `PlaybackPlan`

`plan_playback()` produces an immutable plan containing:

- `server_mode`: `direct` or `server_transcode`;
- `client_decoder`: the effective mpv decoder setting;
- the reason for the decision;
- source FPS/bitrate evidence;
- per-cell demuxer and readahead budget context.

A client software fallback changes only `client_decoder`; it does not silently
change the server mode.

### `ResourceGovernor`

Transcoded plans acquire an idempotent lease keyed by the Emby play-session
identity. Direct plans do not consume a lease. Leases are released only after
the session broker receives a successful stop response, so failed cleanup is
fail-closed instead of allowing a transcode stampede.

### `EmbySessionBroker`

The broker owns active and pending session records, bounded eviction, outage
deferral, stop idempotence, bounded retry, and shutdown enumeration. The wall
retains compatibility delegates (`_register_session()` and
`stop_emby_session()`), but no longer owns the registry dictionaries or stop
worker implementation.

### `CellPlaybackController`

The controller accepts immutable `PlaybackIdentity` values derived from the
existing native context tuple. A stale callback is rejected without changing
state. Global shutdown is terminal and is allowed to override a stale event
identity. `VideoCell` currently observes this controller while retaining the
proven native command/lock ordering; further native adapter extraction can
therefore be incremental.

## Evidence emitted by stats and soak runs

Per-cell stats now include `playback_state` and a credential-free
`playback_plan`. Aggregate stats include `playback_policy` (including both
per-cell and aggregate cache ceilings) and a broker snapshot
(`active_sessions`, `pending_stops`, `inflight_stops`, and active transcode
count). The soak manifest preserves `HYPERWALL_AUTO_TRANSCODE`, records the
expected cell count, and includes both peak and current resident-memory
measurements when the platform exposes them. `analysis.json` is written before
the redacted artifact tree is created.

## Migration rules

1. Add or test policy decisions in the pure plan module first.
2. Do not infer server mode from URL text in new code.
3. Do not use client `hwdec` state as evidence of server transcoding.
4. Route new session admission and cleanup through the broker.
5. Keep native Qt/libmpv calls behind the existing cell ownership locks until a
   separately tested native adapter is ready.
6. Pass `--expected-cells` for every controlled soak; a directory name is not
   evidence of the actual grid size.
7. Treat the macOS GUI suites and the full repository runner as the
   acceptance gate for each migration slice.
