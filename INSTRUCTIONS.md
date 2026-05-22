# HyperWall 8.1 — Setup & Behavior

Active v8/v8.1 runtime. Same `config.ini`. Backend is now `python-mpv` (libmpv); the Qt media stack is gone. Legacy v7.4 is quarantined under `legacy/hyperwall_v7_4.py` so normal launch paths cannot accidentally run it.

---

## What's new vs. 7.4

| Area | 7.4 | 8.0 |
|---|---|---|
| Decoder | Qt6 ffmpeg / D3D11 swapchain | libmpv, `vo=gpu-next`, `hwdec=nvdec` |
| Stream tiers | DIRECT / REMUX / TRANSCODE (3-tier routing) | **Always-REMUX** (one path; force-transcode only on retry-2 escalation) |
| Bad-audio set | Maintained `_BAD_AUDIO` for TrueHD/DTS-HD | Gone — Emby always re-muxes audio to AAC stereo |
| Codec env vars | `QT_FFMPEG_*` env block | Removed; mpv handles HW selection |
| Position update throttle | Manual 250 ms gate in `_on_position` | mpv `time-pos` observer fires at its own (low) rate |
| Retry escalation | Same 3-retry exponential backoff | Same; `_force_transcode` now flips `VideoCodec=copy` → `VideoCodec=h264` |
| Process isolation | None | Bundled `hyperwall_v8.exe` so NVIDIA driver applies a per-app G-Sync-off profile |

UX is intentionally close to 7.4, with one important audio correction: there is no global mute/unmute shortcut. Audio is controlled per cell only, and multiple cells may be unmuted at the same time. Shortcuts are `C/Space/F/A/S/R/Esc`; the controls strip, title overlay, and cleanup-on-startup flow remain.

---

## First-time setup

### 1. One-shot bootstrap (recommended)

```powershell
pwsh -ExecutionPolicy Bypass -File .\bootstrap_v8.ps1
```

Use `pwsh` (PowerShell 7+), not `powershell` (5.1) -- 5.1's cp1252 decoding chokes on UTF-8 scripts. The script does steps 2-4 below, runs the build, and prints a diagnostics block at the end.

If you'd rather do it manually, the steps are:

### 1a. Dependencies (manual)

```cmd
pip install python-mpv pyqt6 requests pyinstaller
```

### 2. libmpv DLL

The bootstrap script auto-fetches `mpv-dev-x86_64-v3-*.7z` from <https://github.com/zhongfly/mpv-winbuild/releases/latest> and extracts `mpv-2.dll` via `py7zr`. If you're doing this manually, download that archive, extract, and place `mpv-2.dll` next to `hyperwall_v8.py`.

### 3. NVIDIA Profile Inspector

Download the latest release from <https://github.com/Orbmu2k/nvidiaProfileInspector>. Unzip and place at:

```
<wall_dir>\tools\nvidiaProfileInspector.exe
```

(Where `<wall_dir>` is the directory where HyperWall is installed, e.g. `C:\Apps\hyperwall`. Or simply drop `nvidiaProfileInspector.exe` next to `hyperwall_v8.exe` — the auto-discovery logic finds it there without any path editing.)

### 4. Build the launcher exe

```cmd
cd <wall_dir>
build_v8.bat
```

Produces `hyperwall_v8.exe` in the same dir.

### 5. First run

Launch `hyperwall_v8.exe` directly (not via `python hyperwall_v8.py`). On first run it will UAC-prompt to silent-import `hyperwall_v8.nip` into the NVIDIA driver. Approve once. A sentinel file is written with the driver version.

### 6. Update / use the shortcut

`launch.bat` starts v8 only. It launches `hyperwall_v8.exe` when the bundled exe is current; if the exe is older than checked-out source, it warns and falls back to `python hyperwall_v8.py`. The legacy v7.4 monolith is not launched by this batch file.

Point your desktop shortcut (e.g. `hyperwall.lnk`) at either `hyperwall_v8.exe` directly or `launch.bat` inside `<wall_dir>`.

---

## G-Sync isolation — how the non-fragile bit works

NVIDIA driver profiles match by executable basename. Generic `python.exe` profiles would touch every Python program on the machine — fragile. Instead:

1. PyInstaller bundles the script into `hyperwall_v8.exe` — a unique basename only HyperWall uses.
2. `hyperwall_v8.nip` targets that exact basename, sets:
   - **VRR Mode = Fully disabled** (no G-Sync hunting between mixed-FPS cells)
   - **VRR Requested State = Off** (kernel-level VRR disable for this app)
   - **Power management = Prefer maximum performance** (no clock-gating mid-wall)
   - **Threaded optimization = On** (helps with 12 concurrent decode contexts)
3. On every launch, `hyperwall_v8.py` reads `nvidia-smi` for the current driver version and compares it against `.hyperwall_v8_nvprofile.sentinel`. Match → no-op (silent). Mismatch (= driver was reinstalled and wiped custom profiles) → UAC-prompts NPI's `-silentImport` to reapply, writes new sentinel.
4. If you launch via `python hyperwall_v8.py` instead of the exe, the script logs a warning and continues — isolation is just disabled, nothing breaks.

Verify after first apply: open NVIDIA Profile Inspector → search "HyperWall" in the profile dropdown → confirm VRR Mode = Fully disabled and the executable list contains `hyperwall_v8.exe`.

---

## Hardware tuning (Blackwell B200/B100 + 240 Hz UltraGear)
- Monitor: LG 27" 240 Hz UltraGear (native 240 Hz, G-Sync Compatible, HDR400)
- GPU: NVIDIA Blackwell (nvdec / CUDA offload, d3d11 + gpu-next)
- `vo=gpu-next`, `gpu_api=d3d11`, `hwdec=nvdec`
- `profile=fast`, `video-sync=display-resample`, `interpolation=no`
- `target_colorspace_hint=yes` — HDR hinting
- `cache=yes`, `cache-secs=10`, `demuxer_max_bytes=256MiB`, `demuxer_readahead_secs=20` — generous for 4K remux bursts on 32 GB RAM
- `network_timeout=15`, `stream_lavf_o=reconnect=1,...`
- `ao=wasapi`, `audio_buffer=1.0`
- All values chosen for low-latency 240 Hz playback with HDR.


---

## Files in this drop

| File | Purpose |
|---|---|
| `hyperwall_v8.py` | The deliverable. Run via `python hyperwall_v8.py` for dev, or `hyperwall_v8.exe` for production. |
| `hyperwall_v8.nip` | NVIDIA Profile Inspector profile. Targets `hyperwall_v8.exe` only. |
| `build_v8.bat` | PyInstaller one-file build. Bundles `mpv-2.dll`. |
| `tools/nvidiaProfileInspector.exe` | You install this once. HyperWall calls it when sentinel is stale. |
| `.hyperwall_v8_nvprofile.sentinel` | Auto-managed. Holds the driver version of the last profile apply. |

---

## Smoke tests (per brief)

### Static repo guards

After pulling a branch, run the no-dependency guard suite before building:

```powershell
python .\tests\run_repo_guards.py
```

These checks prevent the specific v8 footguns we already hit: root `hyperwall.py` returning, `launch.bat` pointing at legacy code, global `M` mute returning, missing Escape emergency filter, or missing runtime identity logging.

### Manual wall smoke

Run after build, in order:

1. Launch → wizard appears with last selections preselected
2. Set `cleanup_on_startup=true`, tag a clip, relaunch → progress dialog → wizard → wall
3. Wall comes up across selected monitors, all cells start within ~4 s
4. `C` toggles controls; fade is smooth
5. Click anywhere on seek bar → playhead jumps there
6. Speaker button and volume slider affect only that cell; unmuting one cell does not mute any other cell
7. Multiple cells can remain unmuted simultaneously
8. `F` → favorites only; `A` → all
9. Trash button on a clip → tag added (verify in Emby UI)
10. Star button → favorite added
11. Mouse idle 3 s → cursor disappears; move → reappears
12. 4K source plays without frame drops (Emby transcodes it down to 1080p server-side via REMUX path)
13. Two simultaneous heavy sources play without stutter
14. **Video transitions visibly smoother than 7.4** — this is the new bar; mpv's pre-buffered loadfile + libmpv decoder reuse should make end-of-clip → next-clip near-seamless

If any regress vs. 7.4, that's a release blocker per the brief.

---

## Known-uncertain items (flag if seen)

- **`vo=gpu-next` stability** on your specific 32.0.15.9636 driver — Blackwell + gpu-next is recent; if you see hangs at startup, change `MPV_OPTS["vo"]` from `"gpu-next"` to `"gpu"` and report.
- **Title overlay z-order over mpv wid** — Qt child widget over a native HWND is reliable on Win11 but worth eyeballing. If the overlay vanishes behind the video, fallback is mpv's own OSD via `show-text` (would need a small refactor).
- **NPI setting IDs** for VRR — I used the documented IDs but they have shifted between NPI versions before. After first import, open NPI and confirm the `HyperWall` profile shows VRR Mode = Fully disabled. If it doesn't, manually toggle it once in NPI and re-export the .nip.

---

## Rollback

7.4 is preserved as `legacy/hyperwall_v7_4.py` for archaeology only. Normal launcher/build paths are v8-only. To roll back deliberately, run `python legacy/hyperwall_v7_4.py` after confirming the old monolith still matches your current `config.ini` and dependency set.
