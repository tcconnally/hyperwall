# HyperWall 8.2 — one-shot bootstrap
# Run from pwsh (PowerShell 7+):  pwsh -ExecutionPolicy Bypass -File .\bootstrap_v8.ps1
$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "==================== HYPERWALL 8.2 BOOTSTRAP ====================" -ForegroundColor Cyan
Write-Host ""

# ── 1. Python check ──────────────────────────────────────────────────
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "[FAIL] Python not found. Install Python 3.8+ from python.org" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] Python: $(py --version 2>&1)" -ForegroundColor Green

# ── 2. Python deps ───────────────────────────────────────────────────
Write-Host "[*] Installing Python dependencies..."
py -m pip install --quiet python-mpv pyqt6 requests flask pyinstaller py7zr
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] pip install failed" -ForegroundColor Red
    exit 1
}
Write-Host "[OK] Python deps installed" -ForegroundColor Green

# ── 3. mpv-2.dll ─────────────────────────────────────────────────────
if (-not (Test-Path "$ScriptDir\mpv-2.dll") -and -not (Test-Path "$ScriptDir\libmpv-2.dll")) {
    Write-Host "[*] Downloading mpv-2.dll from shinchiro builds..."
    # Clean stale leftovers from a prior failed run
    Remove-Item "$ScriptDir\mpv-temp.7z" -ErrorAction SilentlyContinue
    Remove-Item "$ScriptDir\_extract_mpv.py" -ErrorAction SilentlyContinue
    Get-ChildItem "$ScriptDir" -Directory | Where-Object Name -match '^mpv' | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    $ghUrl = "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases"
    try {
        $release = Invoke-RestMethod -Uri $ghUrl -TimeoutSec 15 | Select-Object -First 1
        $asset = $release.assets | Where-Object name -match 'mpv-dev-x86_64.*\.7z$' | Select-Object -First 1
        if (-not $asset) { throw "No 7z asset found" }
        Write-Host "  Found: $($asset.name) ($([math]::Round($asset.size/1MB,1)) MB)"
        Write-Host "  Downloading..."
        $sevenZip = "$ScriptDir\mpv-temp.7z"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $sevenZip -TimeoutSec 120
        Write-Host "  Extracting..."
        # Write a temp Python script to avoid backslash-escaping hell
        # with inline -c + Windows paths.
        $extractScript = @"
import py7zr, shutil, sys, os
src = r'$sevenZip'
dst = r'$ScriptDir'
a = py7zr.SevenZipFile(src)
a.extract(path=dst)
a.close()
"@
        $extractScript | Out-File -Encoding utf8 "$ScriptDir\_extract_mpv.py"
        py "$ScriptDir\_extract_mpv.py"
        Remove-Item "$ScriptDir\_extract_mpv.py" -ErrorAction SilentlyContinue
        $dll = Get-ChildItem "$ScriptDir" -Filter "mpv-2.dll" -Recurse -File | Select-Object -First 1
        if ($dll) {
            Move-Item $dll.FullName "$ScriptDir\mpv-2.dll" -Force
        } else {
            # shinchiro ships as libmpv-2.dll
            $dll = Get-ChildItem "$ScriptDir" -Filter "libmpv-2.dll" -Recurse -File | Select-Object -First 1
            if ($dll) { Move-Item $dll.FullName "$ScriptDir\libmpv-2.dll" -Force }
        }
        Remove-Item "$ScriptDir\mpv-temp.7z" -ErrorAction SilentlyContinue
        # Clean extracted dirs
        Get-ChildItem "$ScriptDir" -Directory | Where-Object Name -match 'mpv' | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "[OK] mpv-2.dll installed" -ForegroundColor Green
    } catch {
        Write-Host "[WARN] Could not auto-download mpv DLL: $_" -ForegroundColor Yellow
        Write-Host "  Manual download: https://github.com/shinchiro/mpv-winbuild-cmake/releases/latest"
        Write-Host "  Extract mpv-2.dll or libmpv-2.dll to: $ScriptDir"
    }
} else {
    Write-Host "[OK] mpv-2.dll already present" -ForegroundColor Green
}

# ── 4. NVIDIA Profile Inspector ───────────────────────────────────────
$npiPath = "$ScriptDir\tools\nvidiaProfileInspector.exe"
if (-not (Test-Path $npiPath)) {
    # Check next to script dir
    if (Test-Path "$ScriptDir\nvidiaProfileInspector.exe") {
        $npiPath = "$ScriptDir\nvidiaProfileInspector.exe"
    } else {
        Write-Host "[*] Downloading NVIDIA Profile Inspector..."
        try {
            $npiUrl = "https://api.github.com/repos/Orbmu2k/nvidiaProfileInspector/releases"
            $release = Invoke-RestMethod -Uri $npiUrl -TimeoutSec 15 | Select-Object -First 1
            $asset = $release.assets | Where-Object name -match '\.zip$' | Select-Object -First 1
            if (-not $asset) { throw "No zip asset found" }
            New-Item -ItemType Directory -Path "$ScriptDir\tools" -Force | Out-Null
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile "$ScriptDir\tools\npi-temp.zip" -TimeoutSec 60
            Expand-Archive "$ScriptDir\tools\npi-temp.zip" "$ScriptDir\tools" -Force
            Remove-Item "$ScriptDir\tools\npi-temp.zip" -ErrorAction SilentlyContinue
            $npiExe = Get-ChildItem "$ScriptDir\tools" -Filter "nvidiaProfileInspector.exe" -Recurse -File | Select-Object -First 1
            if ($npiExe) {
                Move-Item $npiExe.FullName $npiPath -Force
                Write-Host "[OK] NVIDIA Profile Inspector installed" -ForegroundColor Green
            } else {
                throw "nvidiaProfileInspector.exe not found in archive"
            }
        } catch {
            Write-Host "[WARN] Could not auto-download NPI: $_" -ForegroundColor Yellow
            Write-Host "  Manual download: https://github.com/Orbmu2k/nvidiaProfileInspector/releases/latest"
            Write-Host "  Extract nvidiaProfileInspector.exe to: $ScriptDir\tools\"
        }
    }
} else {
    Write-Host "[OK] NVIDIA Profile Inspector already present" -ForegroundColor Green
}

# ── 5. Build ──────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[*] Building hyperwall_v8.exe..."
cmd /c "$ScriptDir\build.bat"
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] Build failed" -ForegroundColor Red
    exit 1
}

# ── 6. Diagnostics ────────────────────────────────────────────────────
Write-Host ""
Write-Host "==================== BOOTSTRAP COMPLETE ====================" -ForegroundColor Cyan
Write-Host ""
$dll = if (Test-Path "$ScriptDir\mpv-2.dll") { "mpv-2.dll" } elseif (Test-Path "$ScriptDir\libmpv-2.dll") { "libmpv-2.dll" } else { "MISSING" }
Write-Host "  Python: $(py --version 2>&1)"
Write-Host "  mpv DLL: $dll"
Write-Host "  NPI: $(if(Test-Path $npiPath){'present'}else{'missing'})"
Write-Host "  Flask: $(py -c 'import flask; print(flask.__version__)' 2>&1)"
Write-Host "  Exe: $(if(Test-Path "$ScriptDir\hyperwall_v8.exe"){'hyperwall_v8.exe built'}else{'NOT BUILT'})"
Write-Host ""
Write-Host "  Launch: .\hyperwall_v8.exe" -ForegroundColor Green
Write-Host ""
