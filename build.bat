@echo off
:: Hyperwall v9 — PyInstaller one-file build
:: Produces hyperwall_v8.exe with embedded mpv-2.dll

setlocal
cd /d "%~dp0"

echo === Hyperwall v9 Build ===
echo.

:: Verify dependencies
python -c "import PyQt6; import requests; import flask" 2>nul
if %errorlevel% neq 0 (
    echo Installing build dependencies...
    pip install pyqt6 requests flask pyinstaller
)

:: Verify mpv DLL
if not exist "mpv-2.dll" (
    if exist "libmpv-2.dll" (
        echo Renaming libmpv-2.dll to mpv-2.dll...
        ren "libmpv-2.dll" "mpv-2.dll"
    ) else (
        echo WARNING: mpv-2.dll not found. Run bootstrap.ps1 first.
        echo Download from: https://github.com/shinchiro/mpv-winbuild-cmake/releases/latest
        echo Extract libmpv-2.dll and place in this directory.
        echo.
    )
)

echo Building hyperwall_v8.exe...
pyinstaller ^
    --onefile ^
    --name hyperwall_v8 ^
    --add-data "mpv-2.dll;." ^
    --add-data "hyperwall.nip;." ^
    --console ^
    --clean ^
    hyperwall.py

if %errorlevel% equ 0 (
    echo.
    echo === Build Complete ===
    echo Output: dist\hyperwall_v8.exe
    echo Copy it to the repo root to use with NVIDIA G-Sync isolation.
    copy /y "dist\hyperwall_v8.exe" "hyperwall_v8.exe"
) else (
    echo.
    echo === Build Failed ===
    exit /b 1
)
