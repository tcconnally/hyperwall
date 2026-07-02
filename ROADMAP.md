# Hyperwall Roadmap — the road to v10

> Deep-dive review of v9.0.0 (3,069 LOC, 11 modules) and the plan to take
> Hyperwall to the next level. Every task carries forward hard-won lessons
> as guardrails so we don't relearn them.

## Where v9 stands

The v8→v9 ground-up rewrite paid off. The package is clean, typed, and most
classic footguns are already closed:

| Previously flagged | Status in v9 |
|---|---|
| O(n) list-compare for filter state | ✅ Fixed — explicit `filter_mode` (`wall.py:118`) |
| Cross-thread mpv IPC from Flask thread | ✅ Fixed — main-thread `_paused` cache (`web.py:59`) |
| String fallbacks to `getint`/`getboolean` | ✅ Fixed — typed fallbacks (`config.py:56–60`) |
| Unbounded mpv terminate hang | ✅ Fixed — bounded `ThreadPoolExecutor` (`cell.py:312`) |
| Stale observer callbacks after reload | ✅ Fixed — generation counter (`_mpv_gen`) |
| Unauthenticated shutdown | ✅ Fixed — `HYPERWALL_WEB_TOKEN` guard (`web.py:43`) |

**v10 is not a bug-fix release — it's a "next level" release.**

## Findings that still matter (file:line grounded)

- **🔴 Version identity is a landmine.** Package says v9 (`__init__.py:11`) but the
  binary is `hyperwall_v8.exe` everywhere, and G-Sync isolation is hard-gated on
  that exact basename (`nvidia.py:85`). The `v8`/`9.0`/`HyperWall/9.0` string is
  duplicated across `constants.py`, `nvidia.py`, `emby.py:59,88`, `wizard.py`,
  `build.bat`, `build.ps1`, `bootstrap_v8.ps1`, `launch.bat`, and the tests.
  Bumping to v10 will silently break G-Sync isolation unless all references move
  in lockstep.
- **🟠 No stall watchdog.** The retry/escalation chain (`cell.py:785`) fires only
  on EOF or explicit error. A silent mid-stream freeze produces neither — the
  cell sits dead forever. Highest-value reliability gap for a 24/7 wall.
- **🟠 `_switching` leaks on the error path.** Set in `play()` (`cell.py:448`),
  cleared only on the first `eof`. The `reason == "error"` branch (`cell.py:748`)
  returns without clearing it.
- **🟠 Memory scales unbounded with cell count.** `demuxer_max_bytes=512MiB` +
  `cache_secs=30` + `demuxer_readahead_secs=30` are per cell (`constants.py:63–65`).
  A 6×6 grid can reach ~18 GB of demuxer buffer.
- **🟡 Single global playlist** (`wall.py:302–310`) — no per-cell/per-monitor sourcing.
- **🟡 Emby-only.** Jellyfin is one `MediaBackend` abstraction away (`emby.py`).
- **🟡 Shallow tests.** The 7 repo guards (`tests/run_repo_guards.py`) are
  import/structure smoke tests. None of the bug-prone logic (`_build_url`,
  `needs_transcode`, retry transitions, config round-trip) is tested. No CI.
- **🟡 Web remote** polls every 3s (`web.py:277`); dead `esc()` helper (`web.py:276`).

## Guardrails carried into every task

- **`static=true` is load-bearing** — Emby 4.9.5.0 returns HTTP 500 on `/stream`
  without it. Never remove without live `curl` proof against the target instance.
- **Never assume a fix works from code inspection** — validate playback against a
  real Emby server before declaring done.
- **Don't ship speculative behavior** — ground every change in observed code.
- **Build hygiene** — stale exe lags source; `python -m pip` / `python -m PyInstaller`
  on skyhawk; PowerShell ≠ cmd (`Copy-Item -Force`, `$env:VAR`).

---

## The v10 epics

### Epic 1 — Identity Unification (`v8`→`v10`, single source of truth) · foundation, first
Prerequisite for calling it v10 without breaking G-Sync.
1. One canonical `__version__`; derive everything (User-Agent, auth Version, titles).
2. Decouple G-Sync isolation from the literal `hyperwall_v8.exe` basename — gate on
   a `hyperwall` prefix or an explicit `HYPERWALL_ISOLATED=1` the launcher sets
   (`nvidia.py:85`, `maybe_relaunch_in_isolation`).
3. Parameterize `LAUNCHER_EXE` / `NV_SENTINEL` off the version (`constants.py:23,27`).
4. Rename build output → `hyperwall.exe` (drop version suffix) across build scripts.
5. Add a repo-guard test asserting no `_v8`/`9.0` literals survive outside `__init__.py`.

### Epic 2 — 24/7 Reliability & Self-Healing · highest operational value
1. **Per-cell stall watchdog**: track last `time-pos` advance; QTimer every ~5s —
   if not paused and `time-pos` hasn't moved in N seconds, run the existing
   escalation chain via `_on_error()`. No new failure semantics.
2. Fix the `_switching` leak on the error branch (`cell.py:748`).
3. **Memory-aware cache budget**: scale `demuxer_max_bytes`/`cache_secs` by cell
   count under a total ceiling (`HYPERWALL_CACHE_BUDGET_MB`).
4. **Crash-loop guard**: park a repeatedly-failing cell on a "media unavailable"
   card instead of hammering Emby.

### Epic 3 — Test Harness & CI · locks in everything above
1. Mock-only unit tests (no PyQt/mpv/Emby): `_build_url` DIRECT vs TRANSCODE +
   `static=true` assertion, `needs_transcode` boundaries, retry→transcode→skip
   transitions, config save/load round-trip.
2. GitHub Actions: repo guards + unit tests on push (headless Linux).
3. Optional Windows runner doing a PyInstaller smoke build.

### Epic 4 — Multi-Source Walls (per-monitor / per-cell sourcing) · marquee feature
1. Wizard can bind libraries per monitor (default: current global behavior).
2. Refactor the single deque into a `PlaylistManager` keyed by source-group,
   preserving global de-dup within a group.
3. **Scene presets**: save/name/recall `{grid, screens, libraries, filter}` —
   persisted in config, exposed on the web remote and as `/api/scene/<name>`.

### Epic 5 — Backend Abstraction (Emby + Jellyfin) · audience expansion
1. Extract a `MediaBackend` protocol (`authenticate`, `fetch_libraries`,
   `fetch_items`, `build_stream_url`, `needs_transcode`, tag/favorite mutations).
2. `EmbyBackend` = current code verbatim; add `JellyfinBackend`.
3. Keep the Emby `static=true` / 500-bug workaround intact and documented
   per-backend; validate each against a live server before merge.

### Epic 6 — Web Remote 2.0 & Observability · polish + monitorability
1. Replace 3s polling with SSE/WebSocket push; remove dead `esc()` helper.
2. Optional full-auth mode (token gates all control endpoints).
3. `/api/health` + lightweight `/metrics` (cells alive, retry counts, frame-drop
   totals, uptime).
4. Optional schedule: auto on/off windows (ambient-installation use case).

---

## Suggested sequencing

| Release | Contents | Theme |
|---|---|---|
| **v10.0** | Epics 1 + 2 + 3 | Identity clean, self-healing, tested — a rock-solid core |
| **v10.1** | Epic 4 | Multi-source walls + scene presets |
| **v10.2** | Epic 5 | Jellyfin support |
| **v10.3** | Epic 6 | Web Remote 2.0 + observability |
