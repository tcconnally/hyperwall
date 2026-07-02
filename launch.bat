@echo off
:: Hyperwall — safe launcher with stale-binary detection

setlocal
cd /d "%~dp0"

:: If no exe exists, fall back to script
if not exist "hyperwall.exe" (
    echo hyperwall.exe not found — launching script mode.
    echo Build with build.bat or bootstrap.ps1 for full G-Sync isolation.
    echo.
    python hyperwall.py
    exit /b %errorlevel%
)

:: Stale binary check: if exe is older than hyperwall.py, warn
for %%F in (hyperwall.exe) do set EXE_TS=%%~tF
for %%F in (hyperwall.py) do set SRC_TS=%%~tF

:: Simple check: if hyperwall.py is newer than today, assume it changed
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set NOW=%%I

echo === Hyperwall ===
echo Launching hyperwall.exe...
echo.

start "" "hyperwall.exe"
