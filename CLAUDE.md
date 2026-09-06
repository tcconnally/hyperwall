# Hyperwall — hard-won rules

Bug classes that have each bitten this codebase at least once (2026-07-13
audit campaign, v10.2→v10.9). Before writing code that touches these areas,
check the rule. Before claiming one of these is fixed, run the probe.

## Platforms

- **Use the macOS render path only.** Each cell renders via the libmpv render API
  (`vo=libmpv`) into `macembed.MpvGLWidget` (QOpenGLWidget). Do not add a
  native-window embedding path; `constants.macos_mpv_opts()` supplies the
  supported configuration.
- Render API threading (render.h): the update callback fires on an mpv
  thread — bare signal emit only; all `mpv_render_*` calls run on the GUI
  thread with the widget's GL context current; free the render context
  BEFORE `mpv.terminate()` (`VideoCell._destroy_mpv` order is load-bearing).
- Never let `mpv_render_context_render` block the GUI thread on the audio
  clock: `block_for_target_time=False` + `video-timing-offset=0` on darwin.
- libmpv discovery on macOS: `DYLD_FALLBACK_LIBRARY_PATH` must include the
  Homebrew prefix lib dir BEFORE python starts (launch.sh) — setting it
  from inside Python is a no-op.
- libmpv refuses non-C LC_NUMERIC. `mpv_create()` returns NULL under
  e.g. en_US.UTF-8 (mpv player/main.c check_locale) and python-mpv then
  SEGFAULTS in mpv_set_option. app.py forces `setlocale(LC_NUMERIC, "C")`
  early; launch.sh exports it too. If a
  libmpv embed segfaults at 0x48 (NULL handle), check the locale first.
- macOS `ru_maxrss` is bytes; pair its high-water value with current RSS
  samples before calling a retained cache a leak.
- PyQt6 `QOpenGLContext.getProcAddress` wants bytes/QByteArray, not str —
  and ANY exception inside a ctypes callback is swallowed by the FFI, so
  libmpv gets a garbage GL function pointer and bus-errors when it calls
  it. The macembed `_resolve` callback must be total (try/except → 0).
- Qt **qFatals (SIGABRT) on a cross-thread `QOpenGLWidget.makeCurrent`** —
  and the wall's shutdown terminates cells on a ThreadPoolExecutor, so
  `MpvGLWidget.release()` must only free synchronously on the widget's own
  thread; off-GUI callers queue the free (best-effort at exit). Mid-session
  destroys are GUI-thread, so only shutdown ever hits this.
- The whole teardown chain is only as strong as its weakest raise: a
  TypeError in `release()` (bad `QTimer.singleShot` overload — PyQt6 has
  NO (msec, receiver, slot) form; use a queued pyqtSignal for
  cross-thread hops) aborted cell teardown, mpv.terminate never ran, and
  the live vo thread fired the update callback into a dying widget →
  segfault in `pyqtBoundSignal_emit`. Rules: `release()` NEVER raises;
  the mpv update callback is a total function gated by an
  `_accepting_frames` flag set False FIRST at release; never store a raw
  `signal.emit` as `MpvRenderContext.update_cb`; clear `update_cb = None`
  before freeing the context.
- render.h ordering vs the bounded-terminate pool is a structural
  conflict: the pool terminates cores while the GUI thread blocks in
  `concurrent.futures.wait`, so a free QUEUED from a pool thread to the
  GUI thread can never run before terminate → core destroyed with a live
  render context → SIGABRT. Resolution: `wall._cleanup` frees ALL render
  contexts synchronously on the GUI thread (native windows still alive)
  BEFORE submitting cell releases to the pool. Queued-free remains only
  as the fallback for hypothetical off-GUI callers.

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
- Emby writes/executions must be **verified before logging success**. Read the
  response status back before reporting a server-side change as complete.
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

## Freeze visibility

- The freeze class users actually see is **paused-for-cache** (network
  starvation): invisible to the frame-drop counters and shorter than the
  20s stall watchdog. It is observed per-cell (BUFFERING card + WARNING
  log with duration + freezes/freeze_seconds in the stats dump). If a
  freeze report arrives with clean drop counters, check the FREEZE lines
  first. `cache-pause-wait=3` makes mpv resume only with 3s of buffer —
  one longer pause instead of a freeze-flicker loop.

## Process / repo

- The macOS launch path is `launch.sh`; it must establish the Homebrew
  `libmpv` search path before Python starts. Do not bypass it for playback
  tests.
- This working copy is **shared** between the owner's terminal and agent
  sessions — verify `git branch --show-current` immediately before every
  commit; owner pulls can switch HEAD mid-task.
- The version literal is pinned in `tests/run_repo_guards.py::test_02` —
  bump both together.
- Verification style: use the pure-logic suite and compile check locally, then
  run a short target-Mac pilot before a long playback soak. Never auto-launch
  the wall from a headless verification command.
