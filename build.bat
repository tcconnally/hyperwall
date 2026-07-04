@echo off
:: Hyperwall — PyInstaller one-file build
:: Produces hyperwall.exe with embedded mpv-2.dll (if present)

setlocal
cd /d "%~dp0"

echo === Hyperwall Build ===
echo.

:: Resolve a Python interpreter. Prefer the 'py' launcher, then fall back to
:: 'python' / 'python3'. IMPORTANT: probe with `where` (a real exe, safe to
:: redirect) instead of running the interpreter under `>nul`. The Microsoft
:: Store App Execution Aliases for py/python (used by the 3.14 Install
:: Manager) return a nonzero exit code when their stdout is redirected to
:: nul inside a batch, which would make a working interpreter look absent.
set "PY="
:: Honor an interpreter handed in by bootstrap.ps1 (it already validated one),
:: so the build doesn't hinge on a second PATH probe from cmd that can miss
:: Python installs the PowerShell session resolves fine (e.g. the 3.14 Install
:: Manager / app-execution aliases).
if defined HYPERWALL_PY set "PY=%HYPERWALL_PY%"
if not defined PY (where py >nul 2>nul && set "PY=py -3")
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (where python3 >nul 2>nul && set "PY=python3")
if not defined PY (
    echo ERROR: No Python interpreter found on PATH.
    echo.
    echo Install Python 3 from https://www.python.org/downloads/ and make sure
    echo "Add python.exe to PATH" is checked during setup, then open a NEW
    echo terminal and re-run build.bat.
    exit /b 1
)
echo Using interpreter: %PY%

:: Verify dependencies
%PY% -c "import PyQt6, requests, flask, PyInstaller" 2>nul
if %errorlevel% neq 0 (
    echo Installing build dependencies...
    %PY% -m pip install pyqt6 requests flask pyinstaller
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

echo Building hyperwall.exe...
%PY% -m PyInstaller ^
    --onefile ^
    --name hyperwall ^
    %DLL_FLAG% ^
    --add-data "hyperwall.nip;." ^
    --console ^
    --clean ^
    hyperwall.py

if %errorlevel% equ 0 (
    echo.
    echo === Build Complete ===
    echo Output: dist\hyperwall.exe
    copy /y "dist\hyperwall.exe" "hyperwall.exe"
    if errorlevel 1 (
        echo WARNING: Could not copy exe to repo root.
    ) else (
        echo Copied to hyperwall.exe in repo root.
    )
) else (
    echo.
    echo === Build Failed ===
    exit /b 1
)
