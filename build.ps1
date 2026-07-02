<# Hyperwall v9 — PyInstaller one-file build (PowerShell) #>

$ErrorActionPreference = "Stop"

Write-Host "=== Hyperwall v9 Build ===" -ForegroundColor Cyan
Write-Host ""

# Resolve a Python interpreter. Prefer the 'py' launcher (guaranteed on PATH
# from a python.org install), then fall back to 'python' / 'python3'. Each
# candidate is an exe plus a (possibly empty) fixed prefix of arguments.
$pyExe  = $null
$pyArgs = @()
foreach ($cand in @(
        @{ exe = "py";      pre = @("-3") },
        @{ exe = "python";  pre = @()     },
        @{ exe = "python3"; pre = @()     })) {
    if (Get-Command $cand.exe -ErrorAction SilentlyContinue) {
        & $cand.exe @($cand.pre + "--version") *> $null
        if ($LASTEXITCODE -eq 0) {
            $pyExe  = $cand.exe
            $pyArgs = $cand.pre
            break
        }
    }
}
if (-not $pyExe) {
    Write-Host "ERROR: No Python interpreter found on PATH." -ForegroundColor Red
    Write-Host ""
    Write-Host "Install Python 3 from https://www.python.org/downloads/ and make sure"
    Write-Host '"Add python.exe to PATH" is checked during setup, then open a NEW'
    Write-Host "terminal and re-run build.ps1."
    exit 1
}
Write-Host "Using interpreter: $pyExe $($pyArgs -join ' ')"

# Verify dependencies
& $pyExe @($pyArgs + @("-c", "import PyQt6, requests, flask, PyInstaller")) 2>$null
$depsOk = ($LASTEXITCODE -eq 0)

if (-not $depsOk) {
    Write-Host "Installing build dependencies..." -ForegroundColor Yellow
    & $pyExe @($pyArgs + @("-m", "pip", "install", "pyqt6", "requests", "flask", "pyinstaller"))
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
    $pyinstallerArgs = @(
        "--onefile",
        "--name", "hyperwall_v8",
        "--add-data", "mpv-2.dll;.",
        "--add-data", "hyperwall.nip;.",
        "--console",
        "--clean",
        "hyperwall.py"
    )
}

& $pyExe @($pyArgs + @("-m", "PyInstaller") + $pyinstallerArgs)

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
