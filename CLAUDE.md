# Hyperwall — hard-won rules

Bug classes that have each bitten this codebase at least once (2026-07-13
audit campaign, v10.2→v10.9). Before writing code that touches these areas,
check the rule. Before claiming one of these is fixed, run the probe.

## python-mpv API

- `m["name"]` reads/writes **options/**`name`, NOT the property. Property-only
  names (`eof-reached`, `time-pos`, `paused-for-cache`, `core-idle`, stats
  counters, `audio-params`) **raise** on `m[...]` — use attribute access
  (`m.eof_reached`). Option-aliased names (`pause`, `mute`, `volume`, `aid`,
  `loop-file`) work either way. A raise inside a `try/except: return`
  silently killed every EOF advance for a full release.
- Exceptions inside property-observer/event callbacks are **swallowed** by
  python-mpv's event loop. Observer bodies must be bare signal emits or
  trivial assignments.
- `end-file` reasons come from `ev.as_dict()["reason"]` as **bytes**
  (`b'eof'`, `b'stop'`); `ev.event` does not exist in python-mpv 1.x.
- `duration` (and other demuxer-fed properties) re-notify continuously on
  network streams (~50×/s), not once per file. Observer work must be O(1).
- With `keep_open="always"`: natural EOF fires **no** `end-file` — only the
  `eof-reached` property flips; the EOF pause **persists across the next
  loadfile** (explicit unpause required); `loadfile <url>` (replace) clears
  the playlist tail; `playlist-next` emits `end-file reason=stop` for the
  old track.

## mpv playback semantics

- **Never demux a problem file's audio at load.** Muted cells load `aid=no`;
  some poorly-interleaved files hard-freeze (`paused-for-cache`) if their
  audio stream is demuxed from the start — independent of `video-sync` mode.
  Arming audio **mid-stream** on unmute is safe. (v10.8 armed audio at load
  for seamless unmute and froze passive playback; reverted in v10.9.1.)
- Unmute relock is a **keyframe** seek (`absolute+keyframes`): exact seeks
  re-decode to the position (~1s freeze); no seek at all stutters until the
  audio buffer fills (`video_sync=audio` follows the cold track).
- Downscaling IS the wall's picture quality — `dscale=mitchell` +
  `correct-downscaling` are guarded by a repo test. Don't reintroduce
  `profile=fast`.

## ctypes / Win32

- **Declare `restype`/`argtypes` on every Win32 call.** The default `c_int`
  restype truncates 64-bit handles: `GetCurrentProcess()`'s pseudo-handle
  becomes invalid and the next call **fails silently** (returns 0, no
  exception). This shipped twice: the soak resource sampler logged zeros for
  an hour; `SetPriorityClass` was a no-op from v9 to v10.9 while the log
  claimed HIGH priority.
- Check Win32 return values; log a WARNING on failure. Never log success
  you didn't verify (read the state back where cheap).
- HWNDs from `winId()` need the `& 0xFFFFFFFF` mask before handing to mpv.

## Qt

- `WallController` is a **plain object**, not a QObject — never pass it as a
  Qt parent (crashed startup once). Parentless QObjects need a surviving
  Python reference.
- **Never toggle child visibility under a QGraphicsOpacityEffect** — the
  effect's cached render misses newly-shown children on the live wall
  (offscreen `grab()` forces a full render and hides the bug). Design rows
  static; vary state via QSS dynamic properties + unpolish/polish.
- mpv callbacks run on mpv threads; Flask handlers on worker threads. Only
  bare queued-signal emits / `run_on_main` dispatch may touch Qt state.
- Tests: the first suite to create a Qt app must create a full
  `QApplication` — a bare `QCoreApplication` first hard-aborts later widget
  construction in the same process.
- State written from continuous signals drifts: record *resting* values
  (sliderReleased), not every `valueChanged` sample (`_last_vol` was poisoned
  by mid-drag sweeps once).

## Silent failure / state drift (2026-07-13 full audit)

- **Check HTTP status on every Emby write** (tags, favorites, deletes) — the
  client helpers never raise_for_status, so a 401/500 used to log "updated"
  while the server rejected the write.
- **Paginate Emby item queries** (StartIndex loop until TotalRecordCount) —
  a fixed Limit silently truncated large libraries behind a success log.
- Emby writes/executions must be **verified before logging success**: NPI
  profile import now waits for the process and checks exit code before
  writing the sentinel (a launched-but-failed import used to be cached as
  success until the next driver update).
- Cached UI state must have ONE owner: `controller.controls_visible` is the
  global toggle only (a cell's autohide once cleared it wall-wide);
  `controls_visible` inits False to match hidden bars; seek release restores
  the PRE-drag pause state; paused cells don't auto-advance at EOF (the
  global-resume path re-arms EOF-held cells).
- Programmatic control-state changes route through `_nudge_pill()` (full
  pill repaint under the opacity effect).
- `_flush_stats` detach-swaps the observer-fed dicts before iterating —
  the mpv event thread writes them concurrently.
- A manual/web advance on a parked cell is a deliberate resume: `play()`
  clears `_parked` so the next failure runs the retry chain (it used to be
  swallowed by the parked-guard). The parked card is sticky (no auto-fade).

## Process / repo

- The exe at repo root is the **shortcut artifact** and G-Sync isolation
  keys on the `hyperwall*.exe` basename — always rebuild (`build.ps1`, pwsh 7)
  after merging source changes, and check the log's `Runtime:` banner
  version against `hyperwall/__init__.py.__version__` before diagnosing
  anything. Stale-exe runs have caused three separate false bug hunts.
- This working copy is **shared** between the owner's terminal and agent
  sessions — verify `git branch --show-current` immediately before every
  commit; owner pulls can switch HEAD mid-task.
- The version literal is pinned in `tests/run_repo_guards.py::test_02` —
  bump both together.
- Verification style: probe against the shipped `mpv-2.dll` headlessly
  (offscreen Qt / `vo=null`) before shipping playback changes; the owner's
  rule is **never auto-launch the wall**.
