# =============================================================================
# HyperWall 8.0 -- bootstrap & diagnostics
#
# Run with PowerShell 7+ (pwsh):
#   pwsh -ExecutionPolicy Bypass -File .\bootstrap_v8.ps1
#
# Idempotent. What it does:
#   1. Verify Python + pip
#   2. pip-install python-mpv, pyqt6, requests, pyinstaller
#   3. Download nvidiaProfileInspector.exe to .\tools\ (latest GitHub release)
#   4. Check for mpv-2.dll; if missing, open the libmpv download page and wait
#   5. Run the PyInstaller build (build_v8.bat)
#   6. Print a diagnostics block -- paste it back if anything fails
# =============================================================================

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot
$ProgressPreference = 'SilentlyContinue'

function Write-Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red }

$results = [ordered]@{}

# --- 1. Python ---------------------------------------------------------------
Write-Step "Verifying Python"
$pyExe = $null
foreach ($candidate in @('py','python','python3')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $pyExe = $cmd.Source; break }
}
if (-not $pyExe) {
    Write-Fail "No Python found on PATH. Install from python.org and re-run."
    exit 1
}
$pyVersion = & $pyExe --version 2>&1
Write-Ok "$pyExe -- $pyVersion"
$results['python'] = "$pyExe ($pyVersion)"

# --- 2. pip install dependencies ---------------------------------------------
Write-Step "Installing Python deps"
$pkgs = @('python-mpv', 'PyQt6', 'requests', 'pyinstaller')
& $pyExe -m pip install --upgrade --quiet $pkgs
if ($LASTEXITCODE -ne 0) {
    Write-Fail "pip install failed (exit $LASTEXITCODE)"
    exit 1
}
foreach ($p in $pkgs) {
    $info = & $pyExe -m pip show $p 2>$null | Select-String '^Version:'
    if ($info) {
        $v = ($info -split ':')[1].Trim()
        Write-Ok "$p $v"
        $results["pkg_$p"] = $v
    } else {
        Write-Warn "$p -- version not detected"
    }
}

# --- 3. NVIDIA Profile Inspector ---------------------------------------------
Write-Step "NVIDIA Profile Inspector"
$toolsDir = Join-Path $PSScriptRoot 'tools'
$npiExe   = Join-Path $toolsDir 'nvidiaProfileInspector.exe'
New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
if (Test-Path $npiExe) {
    Write-Ok "Already installed: $npiExe"
} else {
    Write-Host "  Fetching latest release from GitHub..."
    try {
        $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/Orbmu2k/nvidiaProfileInspector/releases/latest' -Headers @{'User-Agent'='HyperWall'}
        $asset = $rel.assets | Where-Object { $_.name -match '\.zip$' } | Select-Object -First 1
        if (-not $asset) { throw "No .zip asset in latest release" }
        $zipPath = Join-Path $env:TEMP "npi_$([guid]::NewGuid()).zip"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zipPath -UseBasicParsing
        Expand-Archive -Path $zipPath -DestinationPath $toolsDir -Force
        Remove-Item $zipPath -Force
        if (Test-Path $npiExe) {
            Write-Ok "Installed $($rel.tag_name) -> $npiExe"
            $results['npi_version'] = $rel.tag_name
        } else {
            Write-Fail "Extracted but nvidiaProfileInspector.exe not found in $toolsDir"
            exit 1
        }
    } catch {
        Write-Fail "Auto-download failed: $_"
        Write-Host "  Manually grab: https://github.com/Orbmu2k/nvidiaProfileInspector/releases/latest"
        Write-Host "  Place nvidiaProfileInspector.exe at: $npiExe"
        exit 1
    }
}

# --- 4. libmpv (mpv-2.dll) ---------------------------------------------------
# Auto-fetch from zhongfly/mpv-winbuild GitHub releases. Prefers the x86_64-v3
# build (AVX2-tuned -- Thomas's 9800X3D supports AVX2). Extracts via the
# official 7-Zip standalone CLI (7zr.exe, ~600 KB) -- py7zr doesn't support
# BCJ2 which mpv-winbuild archives use.
Write-Step "libmpv (mpv-2.dll)"
$dllPath = Join-Path $PSScriptRoot 'mpv-2.dll'
if (Test-Path $dllPath) {
    $size = [math]::Round((Get-Item $dllPath).Length / 1MB, 1)
    Write-Ok "Present: $dllPath ($size MB)"
    $results['mpv_dll'] = "$size MB (existing)"
} else {
    # Ensure 7zr.exe is available (kept in tools/ alongside NPI)
    $sevenZrExe = Join-Path $toolsDir '7zr.exe'
    if (-not (Test-Path $sevenZrExe)) {
        Write-Host "  Fetching 7zr.exe from 7-zip.org..."
        try {
            Invoke-WebRequest -Uri 'https://www.7-zip.org/a/7zr.exe' -OutFile $sevenZrExe -UseBasicParsing
            Write-Ok "Installed 7zr.exe -> $sevenZrExe"
        } catch {
            Write-Fail "Could not download 7zr.exe: $_"
            exit 1
        }
    }

    Write-Host "  Fetching latest mpv-dev release from zhongfly/mpv-winbuild..."
    try {
        $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/zhongfly/mpv-winbuild/releases/latest' -Headers @{'User-Agent'='HyperWall'}
        # Prefer x86_64-v3 (AVX2). Exclude 'lgpl' and 'debug' variants.
        $asset = $rel.assets | Where-Object {
            $_.name -match '^mpv-dev-x86_64-v3-.*\.7z$' -and $_.name -notmatch 'lgpl|debug'
        } | Select-Object -First 1
        if (-not $asset) {
            $asset = $rel.assets | Where-Object {
                $_.name -match '^mpv-dev-x86_64-.*\.7z$' -and $_.name -notmatch 'lgpl|debug|v3'
            } | Select-Object -First 1
        }
        if (-not $asset) { throw "No mpv-dev-x86_64*.7z asset in $($rel.tag_name)" }

        $sevenZ = Join-Path $env:TEMP "mpv-dev_$([guid]::NewGuid()).7z"
        Write-Host "  Downloading $($asset.name) ($([math]::Round($asset.size/1MB,1)) MB)..."
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $sevenZ -UseBasicParsing

        # Extract flat (e = no paths), only the mpv-2.dll / libmpv-2.dll members
        Write-Host "  Extracting mpv-2.dll..."
        $extractOut = & $sevenZrExe e $sevenZ "-o$PSScriptRoot" 'mpv-2.dll' 'libmpv-2.dll' '-r' '-y' 2>&1
        Remove-Item $sevenZ -Force -ErrorAction SilentlyContinue

        # Some builds ship libmpv-2.dll instead of mpv-2.dll -- normalize.
        $alt = Join-Path $PSScriptRoot 'libmpv-2.dll'
        if ((Test-Path $alt) -and (-not (Test-Path $dllPath))) {
            Move-Item -Path $alt -Destination $dllPath -Force
        }

        if (-not (Test-Path $dllPath)) {
            Write-Fail "Extraction did not produce mpv-2.dll. 7zr output:"
            $extractOut | Write-Host
            exit 1
        }
        $size = [math]::Round((Get-Item $dllPath).Length / 1MB, 1)
        Write-Ok "Installed mpv-2.dll ($size MB) from $($asset.name)"
        $results['mpv_dll'] = "$size MB (from $($rel.tag_name))"
    } catch {
        Write-Fail "Auto-fetch failed: $_"
        Write-Host "  Manual fallback: download mpv-dev-x86_64-v3-*.7z from:"
        Write-Host "    https://github.com/zhongfly/mpv-winbuild/releases/latest"
        Write-Host "  Extract mpv-2.dll to: $dllPath"
        exit 1
    }
}

# --- 5. PyInstaller build ----------------------------------------------------
Write-Step "Building hyperwall_v8.exe"
$buildBat = Join-Path $PSScriptRoot 'build_v8.bat'
if (-not (Test-Path $buildBat)) {
    Write-Fail "build_v8.bat missing. Re-pull the v8 drop."
    exit 1
}
& cmd /c $buildBat
if ($LASTEXITCODE -ne 0) {
    Write-Fail "Build failed (exit $LASTEXITCODE) -- see output above."
    exit 1
}
$exePath = Join-Path $PSScriptRoot 'hyperwall_v8.exe'
if (Test-Path $exePath) {
    $size = [math]::Round((Get-Item $exePath).Length / 1MB, 1)
    Write-Ok "Built: $exePath ($size MB)"
    $results['exe'] = "$size MB"
} else {
    Write-Fail "Build reported success but hyperwall_v8.exe not found."
    exit 1
}

# --- 6. Diagnostics block ----------------------------------------------------
Write-Step "Diagnostics -- paste this back if anything misbehaves"
Write-Host ""

$drvOut = & nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>$null
$results['gpu'] = if ($drvOut) { $drvOut.Trim() } else { 'nvidia-smi unavailable' }

$displays = Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue | ForEach-Object {
    $name = ($_.UserFriendlyName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ }) -join ''
    $mfg  = ($_.ManufacturerName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ }) -join ''
    "$mfg $name"
}
$results['displays'] = ($displays -join ' | ')

$sentinel = Join-Path $PSScriptRoot '.hyperwall_v8_nvprofile.sentinel'
$results['nv_sentinel'] = if (Test-Path $sentinel) { (Get-Content $sentinel -Raw).Trim() } else { 'not yet applied' }

$results['nip_present']    = Test-Path (Join-Path $PSScriptRoot 'hyperwall_v8.nip')
$results['script_present'] = Test-Path (Join-Path $PSScriptRoot 'hyperwall_v8.py')
$results['config_present'] = Test-Path (Join-Path $PSScriptRoot 'config.ini')
$results['ps_edition']     = "$($PSVersionTable.PSEdition) $($PSVersionTable.PSVersion)"

Write-Host "--- BEGIN DIAGNOSTICS ---" -ForegroundColor Magenta
$results.GetEnumerator() | ForEach-Object {
    "{0,-22} {1}" -f $_.Key, $_.Value | Write-Host
}
Write-Host "--- END DIAGNOSTICS ---" -ForegroundColor Magenta

Write-Host ""
Write-Host "Next:" -ForegroundColor Cyan
Write-Host "  1. Run hyperwall_v8.exe -- accept the one UAC prompt for NVIDIA profile import."
Write-Host "  2. Walk the 13 smoke tests in INSTRUCTIONS_v8.md."
Write-Host "  3. Update the desktop shortcut to point at hyperwall_v8.exe."
