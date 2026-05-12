# HyperWall — Project Instructions

## What This Is

This file documents the legacy v7.4 monolith (`legacy/hyperwall_v7_4.py`). The active v8/v8.1 runtime lives in `hyperwall_v8.py` plus the `hyperwall/` package; use `INSTRUCTIONS_v8.md` for current launcher/build steps.

HyperWall v7.4 (`legacy/hyperwall_v7_4.py`) is a fullscreen video wall application that streams
content from a local Emby media server across one or more monitors in a
configurable grid. It runs on a single Windows machine in a closed local network.

**Current version:** 7.3  
**Stack:** Python, PyQt6, QMediaPlayer / QAudioOutput / QVideoWidget, requests  
**Displays:** Dual LG ULTRAGEAR+ monitors (8–12 simultaneous streams typical)  
**Config:** `config.ini` — `[Login]` section (server_url, username, password)

### Key Classes

| Class | Role |
|---|---|
| `EmbyAPISession` | Thread-safe HTTP session; wraps all Emby API calls |
| `CleanupWorker` | QThread worker; deletes items tagged `ToDelete` on startup |
| `ContentLoaderThread` | QThread; fetches library metadata async so UI launches immediately |
| `VideoCell` | Single player tile — QMediaPlayer + floating controls overlay |
| `WallController` | Owns all cells, windows, routing logic, and global shortcuts |
| `SetupWizard` | QDialog; shown once per launch to pick displays, libraries, grid |

---

## Environment Assumptions

This is a **single-user, single-machine, air-gapped** deployment. There is no
need to defend against:

- Multiple simultaneous users
- Untrusted network input
- Edge-case screen configurations beyond the two known monitors
- Graceful handling of missing dependencies (fail fast is fine)

Do not add complexity in service of hypothetical users or environments that
don't exist.

---

## Core Design Principles

### 1. Performance First

Every decision should favour throughput and latency over safety margins.

- **HIGH process priority** — already set; do not raise to REALTIME (starves OS with 8+ streams).
- **Hardware decode** — `QT_FFMPEG_DECODING_HW_DEVICE_TYPES=d3d11va,cuda,dxva2`
  is set at startup; do not remove or wrap in a try/except that silently skips it.
- **25 ms stream stagger** — cells start with a 25 ms offset between each. Do
  not increase this. Do not make it configurable unless asked.
- **Three-tier stream routing** — DIRECT ≤80 Mbps (static file), REMUX 80–120
  Mbps (HLS stream-copy, audio→AAC), TRANSCODE >120 Mbps (QSV H264 at 80 Mbps).
  Thresholds live in `WallController`. Do not lower them.
- **Position update throttle** — `_on_position` skips updates <250ms apart to
  reduce UI overhead at 8–12 streams (~360 signals/sec → ~48).
- **No polling timers for UI state** — use player signals (`positionChanged`,
  `mediaStatusChanged`) rather than `QTimer` intervals wherever possible.
- **Background threads for all network I/O** — API calls for tagging, favoriting,
  and content loading must never block the Qt event loop.

### 2. Minimal UI

The wall is meant to be watched, not operated. Controls are hidden by default
and toggled with `C`. When adding UI elements, ask: *would a user ever need this
while the wall is running?* If the answer is rarely, it doesn't belong on screen.

- **No tooltips, status bars, or informational dialogs during playback.**
- **No animations or transitions.** Black gaps between videos are fine.
- **No per-cell labels or overlays unless part of the controls strip.** The
  title label inside the controls frame is sufficient.
- **Controls strip is a bottom overlay** — video fills 100% of the cell via
  absolute geometry; controls float above it. Do not revert to a VBox layout
  that shrinks the video area.
- **Dark theme only.** Background `#0e0e0e`, accent `#3b8edb`. Do not introduce
  additional colours.

### 3. Keep It Simple

Prefer fewer lines over more. Prefer deleting code over adding it. If a feature
request can be satisfied by changing a constant rather than adding a class,
change the constant.

- **No config file migrations.** If a new setting is needed, give it a sensible
  default via `fallback=` in `cfg.get()`; never write a migration routine.
- **No plugin architecture, no abstract base classes, no factory patterns.**
- **No third-party dependencies beyond PyQt6 and requests.** Both are already
  installed.
- **Legacy single file.** v7.4 is quarantined in `legacy/hyperwall_v7_4.py`. Active v8 work belongs in `hyperwall_v8.py` and the `hyperwall/` package; do not recreate a root `hyperwall.py` launcher.

---

## Keyboard Shortcuts (do not remove or reassign)

| Key | Action |
|---|---|
| `C` | Toggle controls visibility on all cells |
| `Space` | Global pause / resume |
| `F` | Filter to favorites only |
| `A` | Reset filter (show all) |
| `Escape` | Shutdown |

Audio is per-cell only: use each cell's speaker button and volume slider. HyperWall intentionally has no global mute/unmute because multiple cells may be unmuted simultaneously.

Shortcuts must work even when embedded media/native child widgets have focus. v8 keeps normal shortcuts registered per fullscreen window and also installs an app-level Escape-only event filter as a last-resort emergency shutdown path.

---

## API Conventions

- All Emby calls go through `EmbyAPISession.get()`, `.post()`, or `.delete()`.
  Do not call `self.api.session.*` directly from outside `EmbyAPISession`.
- Tag updates require a GET-then-POST cycle (Emby's PATCH support is unreliable).
  The helper lives in `WallController.update_tags()`.
- `verify=False` is intentional — the local Emby server uses a self-signed cert.
  Do not add a config option to toggle this.

---

## What Not to Do

- Do not add a system tray icon.
- Do not add a "restore session" dialog — settings are always persisted to
  `config.ini` and pre-selected in the wizard automatically.
- Do not add network retry logic to `EmbyAPISession` itself — retries belong in
  the specific worker that needs them (e.g. `VideoCell._on_error`).
- Do not add logging beyond INFO level in normal operation. Debug logs create
  noise in `hyperwall.log` and slow down hot paths.
- Shutdown uses `QApplication.quit()` — the `os._exit(0)` hard-kill was only
  needed for the overlay layout (7.0/7.1) which caused Qt HWND deadlocks on
  teardown. The VBoxLayout approach shuts down cleanly.

---

## Local directory cleanup

Run `cleanup_wall_dir.ps1 -Apply` after confirming the v8 launcher works; it moves old logs, caches, build leftovers, and the legacy v7 monolith into `_archive/` instead of deleting them.
