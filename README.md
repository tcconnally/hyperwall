# HyperWall

HyperWall is a fullscreen multi-monitor video wall for a local Emby server. The active runtime is the v8.2 rewrite: a small launcher shim (`hyperwall_v8.py`) plus the structured `hyperwall/` package, using `python-mpv`/libmpv instead of Qt's media stack.

The legacy v7.4 monolith is preserved only for archaeology at `legacy/hyperwall_v7_4.py`. Normal launch and build paths are v8-only.

## Current status

- Active branch: `main`
- Active entry points: `launch.bat`, `hyperwall_v8.py`, and built `hyperwall_v8.exe`
- Local-only config: `config.ini` copied from `config.example.ini`
- Legacy root `hyperwall.py`: intentionally absent
- Repo guard suite: `python .\tests\run_repo_guards.py`

## Key behavior constraints

These are intentional and should not regress:

- No global mute/unmute shortcut.
- Audio is controlled per cell only.
- Multiple cells may be unmuted simultaneously.
- ESC must exit reliably, even if a native/mpv child has focus.
- Previous/next, seeking, tagging, favorites, and delete-tag flow must remain reliable.
- Performance under real wall load matters more than adding UI surface area.

Current global shortcuts:

| Key | Action |
|---|---|
| `C` | Toggle controls visibility |
| `Space` | Global pause/resume |
| `F` | Favorites filter |
| `A` | All-items filter |
| `S` | mpv stats overlay on cell 0 |
| `R` | Remix dialog |
| `Esc` | Shutdown |

## Web Remote

HyperWall starts a built-in web server on port 8585 (override with `HYPERWALL_WEB_PORT`).
Open the URL printed at startup on any phone or tablet on the same network to control the wall:

- **Play/pause** all cells globally
- **Skip/previous** per cell
- **Filter** favorites / all
- **Hide/show** controls
- **Shutdown** the wall safely

The page auto-refreshes every 3 seconds. No app install needed — just a browser.

API endpoints under `/api/`: `status`, `pause`, `next/<n>`, `prev/<n>`, `loop/<n>`,
`mute/<n>`, `filter`, `controls`, `shutdown`. All POST except `status` (GET).

## Quick start on Windows

From PowerShell 7+:

```powershell
cd C:\Users\tccon\OneDrive\Documents\scripts\wall
git pull
python .\tests\run_repo_guards.py
```

Expected:

```text
7 repo guard test(s) passed.
```

Create your private config if needed:

```powershell
Copy-Item .\config.example.ini .\config.ini
notepad .\config.ini
```

Then bootstrap/build:

```powershell
pwsh -ExecutionPolicy Bypass -File .\bootstrap_v8.ps1
```

Or, if dependencies and `mpv-2.dll` are already present:

```powershell
.\build_v8.bat
```

Launch:

```powershell
.\launch.bat
```

After a successful rebuild, direct production launch is:

```powershell
.\hyperwall_v8.exe
```

`launch.bat` includes a stale-binary guard: if `hyperwall_v8.exe` is older than the checked-out Python source, it warns and runs `hyperwall_v8.py` instead.

## Files worth knowing

| Path | Purpose |
|---|---|
| `hyperwall_v8.py` | v8 launcher shim; delegates to `hyperwall.main` |
| `hyperwall/` | active v8.2 package |
| `launch.bat` | safe launcher with stale-EXE detection |
| `build_v8.bat` | PyInstaller build for `hyperwall_v8.exe` |
| `bootstrap_v8.ps1` | installs deps/tools/DLL and builds |
| `cleanup_wall_dir.ps1` | archives local junk without deleting it |
| `config.example.ini` | safe template; real `config.ini` is ignored |
| `hyperwall_v8.nip` | NVIDIA Profile Inspector profile targeting `hyperwall_v8.exe` |
| `hyperwall/web.py` | built-in web remote server (Flask) |
| `tests/run_repo_guards.py` | no-dependency guard suite |
| `INSTRUCTIONS_v8.md` | detailed setup, tuning, and smoke-test notes |

## NVIDIA / G-Sync isolation

The built exe gives NVIDIA a unique process basename: `hyperwall_v8.exe`. The included `.nip` profile disables VRR/G-Sync for HyperWall only, avoiding mixed-FPS wall jitter without touching generic `python.exe` behavior.

On first exe launch, HyperWall may request UAC to import the NVIDIA Profile Inspector profile. After that, a local sentinel tracks the driver version and re-applies only when needed.

## Manual smoke test checklist

After build, verify at least:

1. Wizard opens with previous selections.
2. Wall opens on selected monitors and cells begin playback.
3. `M` does nothing global.
4. Per-cell speaker/volume controls affect only that cell.
5. Multiple cells can stay unmuted at once.
6. ESC exits.
7. Previous/next and seeking work.
8. Tag/favorite/delete controls update Emby as expected.
9. Performance is acceptable under real wall load.

## Development workflow

Before building or pushing changes:

```powershell
python .\tests\run_repo_guards.py
```

On Linux/macOS clones, the same check is:

```bash
python3 tests/run_repo_guards.py
python3 -m py_compile hyperwall/*.py hyperwall_v8.py tests/*.py
```

Do not commit local runtime artifacts: `config.ini`, logs, built exe, downloaded tools, DLLs, sentinels, pycache, and stress-test outputs are intentionally ignored.
