@echo off
:: Hyperwall — safe launcher with stale-binary detection

setlocal
cd /d "%~dp0"

if not "%HYPERWALL_SOAK_ACTIVE%"=="1" (
    set HYPERWALL_NO_CONFIG_SAVE=
)

:: If no exe exists, fall back to script
if not exist "hyperwall.exe" (
    echo hyperwall.exe not found — launching script mode.
    echo Build with build.bat or bootstrap.ps1 for full G-Sync isolation.
    echo.
    python hyperwall.py
    exit /b %errorlevel%
)

echo === Hyperwall ===

:: Stale-binary check: warn (don't block) if hyperwall.py is newer than the exe.
:: Delegated to scripts\check_stale.ps1 (exit 2 = stale) so the comparison is
:: real and unit-tested in CI, rather than computing timestamps and ignoring them.
if exist "scripts\check_stale.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\check_stale.ps1" ^
        -Source "hyperwall.py" -Exe "hyperwall.exe"
    if errorlevel 2 (
        echo Continuing with the existing exe. Press Ctrl+C to abort and rebuild.
        timeout /t 3 >nul
    )
)

echo Launching hyperwall.exe...
echo.

start "" "hyperwall.exe"
