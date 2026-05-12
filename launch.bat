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
        "$exe = Get-Item -LiteralPath 'hyperwall_v8.exe'; $src = @(Get-Item -LiteralPath 'hyperwall_v8.py'; Get-ChildItem -LiteralPath 'hyperwall' -Filter '*.py' -File -Recurse) | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if ($src.LastWriteTime -gt $exe.LastWriteTime) { exit 10 } else { exit 0 }"
    if errorlevel 10 set EXE_STALE=1
)

if exist "hyperwall_v8.exe" if not defined EXE_STALE (
    echo Launching bundled hyperwall_v8.exe
    start "" "%CD%\hyperwall_v8.exe"
    exit /b 0
)

if defined EXE_STALE (
    echo WARNING: hyperwall_v8.exe is older than the checked-out source.
    echo Running Python source instead. Rebuild with build_v8.bat when ready.
)

if not exist "hyperwall_v8.py" (
    echo.
    echo ========================================================
    echo  CRITICAL ERROR: hyperwall_v8.py NOT FOUND!
    echo ========================================================
    echo Pull the current HyperWall repo or run bootstrap_v8.ps1.
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

%PY% hyperwall_v8.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ========================================================
    echo  HYPERWALL V8 CRASHED
    echo ========================================================
    pause
)

endlocal
