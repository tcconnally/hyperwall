@echo off
setlocal
cd /d "%~dp0"

echo Starting HyperWall v8...
echo Working Directory: %CD%

if exist "hyperwall_v8.exe" (
    start "" "%CD%\hyperwall_v8.exe"
    exit /b 0
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
