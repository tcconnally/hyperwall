@echo off
setlocal
cd /d "%~dp0"

echo Starting HyperWall v8...
echo Working Directory: %CD%

REM Prefer the bundled exe only when it is at least as new as the Python source.
REM This prevents accidentally testing an old PyInstaller build after git pull.
set EXE_STALE=
if exist "hyperwall_v8.exe" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$exe = Get-Item -LiteralPath 'hyperwall_v8.exe'; $srcItems = @(); if (Test-Path -LiteralPath 'hyperwall.py') { $srcItems += Get-Item -LiteralPath 'hyperwall.py' }; $srcItems += Get-ChildItem -LiteralPath 'hyperwall' -Filter '*.py' -File -Recurse; $src = $srcItems | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if ($src -and $src.LastWriteTime -gt $exe.LastWriteTime) { exit 10 } else { exit 0 }"
    if errorlevel 10 set EXE_STALE=1
)

if exist "hyperwall_v8.exe" if not defined EXE_STALE (
    echo Launching bundled hyperwall_v8.exe
    start "" "%CD%\\hyperwall_v8.exe"
    exit /b 0
)

if defined EXE_STALE (
    echo WARNING: hyperwall_v8.exe is older than the checked-out source.
    echo Running Python source instead. Rebuild with build.bat when ready.
)

if not exist "hyperwall.py" (
    echo.
    echo ========================================================
    echo  CRITICAL ERROR: hyperwall.py NOT FOUND!
    echo ========================================================
    echo hyperwall.py is a tracked repo file. Restore it with:
    echo   git restore --source=HEAD -- hyperwall.py
    echo Then rerun this launcher, or run bootstrap_v8.ps1 after the restore.
    pause
    exit /b 1
)

if not exist "mpv-2.dll" (
    echo.
    echo ========================================================
    echo  CRITICAL ERROR: mpv-2.dll NOT FOUND!
    echo ========================================================
    echo Download libmpv: https://mpv.io/installation/  ^(shobon-mpv builds^)
    echo Place mpv-2.dll next to this script and re-run.
    pause
    exit /b 1
)

set PY=
where py >nul 2>&1 && set PY=py
if "%PY%"=="" where python >nul 2>&1 && set PY=python
if "%PY%"=="" where python3 >nul 2>&1 && set PY=python3

if "%PY%"=="" (
    echo.
    echo ========================================================
    echo  CRITICAL ERROR: Python not found on PATH
    echo ========================================================
    echo Run bootstrap_v8.ps1 from PowerShell 7 or install Python.
    pause
    exit /b 1
)

%PY% hyperwall.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ========================================================
    echo  HYPERWALL V8 CRASHED
    echo ========================================================
    pause
)

endlocal
