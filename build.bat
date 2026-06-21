@echo off
:: Hyperwall v9 — PyInstaller one-file build
:: Produces hyperwall_v8.exe with embedded mpv-2.dll (if present)

setlocal
cd /d "%~dp0"

echo === Hyperwall v9 Build ===
echo.

:: Verify dependencies
python -c "import PyQt6; import requests; import flask" 2>nul
if %errorlevel% neq 0 (
    echo Installing build dependencies...
    pip install pyqt6 requests flask pyinstaller
    if %errorlevel% neq 0 (
        echo ERROR: Failed to install dependencies.
        exit /b 1
    )
)

:: Check mpv DLL
set DLL_FLAG=
if exist "mpv-2.dll" (
    set "DLL_FLAG=--add-data mpv-2.dll;."
    echo mpv-2.dll found — embedding in build.
) else if exist "libmpv-2.dll" (
    echo Renaming libmpv-2.dll to mpv-2.dll...
    ren "libmpv-2.dll" "mpv-2.dll"
    set "DLL_FLAG=--add-data mpv-2.dll;."
    echo mpv-2.dll ready — embedding in build.
) else (
    echo WARNING: mpv-2.dll not found — building WITHOUT embedded DLL.
    echo The exe will need mpv-2.dll placed alongside it to run.
    echo Run bootstrap.ps1 to auto-download, or get it from:
    echo   https://github.com/shinchiro/mpv-winbuild-cmake/releases/latest
    echo Extract libmpv-2.dll, rename to mpv-2.dll, place in this directory.
    echo.
)

echo Building hyperwall_v8.exe...
pyinstaller ^
    --onefile ^
    --name hyperwall_v8 ^
    %DLL_FLAG% ^
    --add-data "hyperwall.nip;." ^
    --console ^
    --clean ^
    hyperwall.py

if %errorlevel% equ 0 (
    echo.
    echo === Build Complete ===
    echo Output: dist\hyperwall_v8.exe
    copy /y "dist\hyperwall_v8.exe" "hyperwall_v8.exe"
    if errorlevel 1 (
        echo WARNING: Could not copy exe to repo root.
    ) else (
        echo Copied to hyperwall_v8.exe in repo root.
    )
) else (
    echo.
    echo === Build Failed ===
    exit /b 1
)
