<# Hyperwall v9 — PyInstaller one-file build (PowerShell) #>

$ErrorActionPreference = "Stop"

Write-Host "=== Hyperwall v9 Build ===" -ForegroundColor Cyan
Write-Host ""

# Verify dependencies
$depsOk = $true
try {
    python -c "import PyQt6; import requests; import flask" 2>$null
} catch {
    $depsOk = $false
}

if (-not $depsOk) {
    Write-Host "Installing build dependencies..." -ForegroundColor Yellow
    pip install pyqt6 requests flask pyinstaller
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to install dependencies." -ForegroundColor Red
        exit 1
    }
}

# Check mpv DLL
$dllFlag = ""
if (Test-Path "mpv-2.dll") {
    $dllFlag = "--add-data mpv-2.dll;."
    Write-Host "mpv-2.dll found — embedding in build." -ForegroundColor Green
} elseif (Test-Path "libmpv-2.dll") {
    Write-Host "Renaming libmpv-2.dll to mpv-2.dll..." -ForegroundColor Yellow
    Rename-Item "libmpv-2.dll" "mpv-2.dll"
    $dllFlag = "--add-data mpv-2.dll;."
    Write-Host "mpv-2.dll ready — embedding in build." -ForegroundColor Green
} else {
    Write-Host "WARNING: mpv-2.dll not found — building WITHOUT embedded DLL." -ForegroundColor Yellow
    Write-Host "The exe will need mpv-2.dll placed alongside it to run."
    Write-Host "Run bootstrap.ps1 to auto-download, or get it from:"
    Write-Host "  https://github.com/shinchiro/mpv-winbuild-cmake/releases/latest"
    Write-Host ""
}

# Build
Write-Host "Building hyperwall_v8.exe..." -ForegroundColor Cyan

$pyinstallerArgs = @(
    "--onefile",
    "--name", "hyperwall_v8",
    "--add-data", "hyperwall.nip;.",
    "--console",
    "--clean",
    "hyperwall.py"
)

if ($dllFlag) {
    $pyinstallerArgs = @("--onefile", "--name", "hyperwall_v8", $dllFlag, "--add-data", "hyperwall.nip;.", "--console", "--clean", "hyperwall.py")
}

& pyinstaller @pyinstallerArgs

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "=== Build Complete ===" -ForegroundColor Green
    Write-Host "Output: dist\hyperwall_v8.exe"
    Copy-Item "dist\hyperwall_v8.exe" "hyperwall_v8.exe" -Force
    if ($?) {
        Write-Host "Copied to hyperwall_v8.exe in repo root."
    } else {
        Write-Host "WARNING: Could not copy exe to repo root." -ForegroundColor Yellow
    }
} else {
    Write-Host ""
    Write-Host "=== Build Failed ===" -ForegroundColor Red
    exit 1
}
