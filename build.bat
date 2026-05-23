@echo off
REM ============================================================================
REM HyperWall 8.2 — PyInstaller build
REM Output: hyperwall_v8.exe (one-file, no console).
REM
REM Why one-file:
REM   - Single bundled exe gives the NVIDIA driver a unique process basename
REM     (hyperwall_v8.exe) for the per-app G-Sync profile to match against.
REM   - mpv-2.dll is bundled inside; no DLL deployment fuss.
REM
REM Requirements (one-time):
REM   pip install pyinstaller python-mpv pyqt6 requests
REM   Place mpv-2.dll next to this script (download from mpv.io, shobon-mpv build).
REM ============================================================================

setlocal
cd /d "%~dp0"

if not exist "hyperwall.py" (
    echo [ERROR] hyperwall.py is missing. It is a tracked repo file.
    echo Restore it with: git restore --source=HEAD -- hyperwall.py
    exit /b 1
)

if not exist "mpv-2.dll" if not exist "libmpv-2.dll" (
    echo [ERROR] No mpv-2.dll or libmpv-2.dll found in %CD%
    echo Download from: https://sourceforge.net/projects/mpv-player-windows/files/libmpv/
    echo   (shinchiro mpv-dev-x86_64 build — extract libmpv-2.dll)
    echo Drop it next to this script and re-run.
    exit /b 1
)

REM Use whichever DLL is present (shinchiro ships as libmpv-2.dll)
if exist "libmpv-2.dll" if not exist "mpv-2.dll" (
    echo [INFO] Renaming libmpv-2.dll ^-^> mpv-2.dll for PyInstaller
    copy /y "libmpv-2.dll" "mpv-2.dll" >nul
)

REM Pick a Python launcher: prefer 'py' (Windows launcher), else 'python'.
set PY=
where py >nul 2>&1     && set PY=py
if "%PY%"=="" where python >nul 2>&1 && set PY=python
if "%PY%"=="" (
    echo [ERROR] No Python on PATH ^(neither 'py' nor 'python'^).
    exit /b 1
)

REM Verify PyInstaller importable as a module ^(survives missing Scripts on PATH^).
%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller not importable.  %PY% -m pip install pyinstaller
    exit /b 1
)

echo [BUILD] Cleaning previous build...
rmdir /s /q build 2>nul
rmdir /s /q dist  2>nul
del /q hyperwall_v8.spec 2>nul

echo [BUILD] Compiling hyperwall_v8.exe...
%PY% -m PyInstaller ^
    --onefile ^
    --noconsole ^
    --name hyperwall_v8 ^
    --add-binary "mpv-2.dll;." ^
    --hidden-import mpv ^
    --collect-submodules PyQt6 ^
    hyperwall.py

if errorlevel 1 (
    echo [ERROR] PyInstaller build failed.
    exit /b 1
)

echo [BUILD] Moving exe to script dir...
move /y "dist\hyperwall_v8.exe" "hyperwall_v8.exe" >nul

echo [BUILD] Cleaning intermediates...
rmdir /s /q build 2>nul
rmdir /s /q dist  2>nul
del /q hyperwall_v8.spec 2>nul

echo.
echo [DONE] hyperwall_v8.exe built.
echo Next: run it once -- it will UAC-prompt to apply the NVIDIA G-Sync profile.
endlocal
