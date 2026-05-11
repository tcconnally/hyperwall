@echo off
:: Set directory to where this script is located
cd /d "%~dp0"

echo Starting HyperWall...
echo Working Directory: %CD%

:: 1. VERIFY FILE EXISTS
if not exist "hyperwall.py" (
    echo.
    echo ========================================================
    echo  CRITICAL ERROR: hyperwall.py NOT FOUND!
    echo ========================================================
    echo.
    echo Please make sure you saved the python code as 'hyperwall.py'
    pause
    exit /b
)

:: 2. RUN SCRIPT
python3 hyperwall.py
if %ERRORLEVEL% EQU 9009 (
    python hyperwall.py
)

:: 3. CATCH CRASHES
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ========================================================
    echo  SCRIPT CRASHED
    echo ========================================================
    pause
)