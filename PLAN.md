# Hyperwall v9 — From-Scratch Rewrite Plan

**Goal:** Complete ground-up rewrite of Hyperwall, keeping the proven tech stack
(python-mpv + PyQt6 + Flask + Emby REST) with clean architecture that eliminates
every known v8 pitfall.

**Architecture:** Single controller tree with strict lifecycle management.
No global state. Every mpv instance creation and destruction goes through a single
path. Thread boundaries are explicit. Config is typed and validated.

**Tech Stack:** Python 3.12+, python-mpv 1.0.7, PyQt6, Flask, requests, PyInstaller

---

## What Changes from v8.2

| Area | v8.2 | v9 |
|---|---|---|
| mpv lifecycle | Scattered create/destroy paths, ThreadPool terminate | Single VideoCell.create/destroy, deterministic cleanup |
| DLL loading | Complex fallback chain with GC-sensitive cookie | Simple single-path DLL registration |
| Config | Raw ConfigParser, string fallbacks everywhere | Typed dataclass, validated on load |
| Error recovery | Per-cell retry with exponential backoff | Same strategy, cleaner implementation |
| Thread model | Implicit thread pools, daemon threads | Explicit thread ownership, bounded shutdown |
| Web remote | Flask in daemon thread, weakref | Same proven approach, cleaner IPC |
| NVIDIA profile | Sentinel-based with ShellExecuteW UAC | Same, with better error reporting |
| Imports | try/except import chains, late imports | Clean imports at module level, optional deps via entry_points |
| Logging | Module-level logger, filter repeated | Same, cleaner MPV log noise filter |
| Type hints | Partial | Full mypy-compatible hints |

## Package Structure

```
hyperwall/
├── hyperwall.py            # Entry point shim (keeps .nip contract: hyperwall_v8.exe basename)
├── hyperwall/
│   ├── __init__.py          # Version
│   ├── app.py               # Application bootstrap + main()
│   ├── config.py            # Typed config, config.ini read/write
│   ├── constants.py         # All tunables, MPV_OPTS, timing values
│   ├── emby.py              # EmbyAPIClient, ContentLoader, CleanupWorker
│   ├── wall.py              # WallController: window grid, shortcuts, pause/filter
│   ├── cell.py              # VideoCell: mpv embed, controls overlay, stats
│   ├── wizard.py            # SetupWizard: monitor + library + grid selector
│   ├── web.py               # Flask web remote on :8585
│   └── nvidia.py            # NVIDIA Profile Inspector integration
├── config.example.ini
├── hyperwall.nip            # NVIDIA profile (unchanged from v8)
├── README.md
├── launch.bat
├── build.bat
├── bootstrap.ps1
└── tests/
    └── test_repo_guards.py
```

## Key Design Decisions

1. **Config is a frozen dataclass** — loaded once, passed down, never mutated
2. **WallController owns everything** — cells, windows, shortcuts, loader
3. **VideoCell lifecycle is `create() → play() → destroy()`** — one path each way
4. **mpv DLL registration happens exactly once** in app bootstrap
5. **All thread work uses explicit ThreadPoolExecutor** with bounded shutdown
6. **Web remote is optional** — graceful degradation if Flask missing
7. **NVIDIA profile is fire-and-forget** — applied once, verified by sentinel
