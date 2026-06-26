# =============================================================================
# Fix Python PATH on skyhawk (Windows 11)
# Root cause of hyperwall build.bat failures: python not on PATH
# =============================================================================
# Run this from PowerShell (Admin NOT required for User PATH changes):
#   pwsh -ExecutionPolicy Bypass -File .\fix_python_path_skyhawk.ps1
# =============================================================================

$ErrorActionPreference = "Stop"
Write-Host "=== Python PATH Fix for skyhawk ===" -ForegroundColor Cyan
Write-Host ""

# ──────────────────────────────────────────────────────────────────
# STEP 1: Find where Python is actually installed
# ──────────────────────────────────────────────────────────────────
Write-Host "[1/5] Locating Python installation..." -ForegroundColor Yellow

$pythonExe = $null
$possiblePaths = @()

# Check common install locations (ordered by likelihood on Win11)
$searchPaths = @(
    "$env:LOCALAPPDATA\Python\pythoncore-3.14-64",
    "$env:LOCALAPPDATA\Python\bin",
    "$env:LOCALAPPDATA\Programs\Python\Python314",
    "$env:LOCALAPPDATA\Programs\Python\Python313",
    "$env:LOCALAPPDATA\Programs\Python\Python312",
    "$env:APPDATA\Python\Python314",
    "C:\Python314",
    "C:\Python313",
    "C:\Python312",
    "$env:USERPROFILE\AppData\Local\Programs\Python\Python314",
    "$env:USERPROFILE\AppData\Local\Programs\Python\Python313"
)

foreach ($dir in $searchPaths) {
    $candidate = Join-Path $dir "python.exe"
    if (Test-Path $candidate) {
        $possiblePaths += $dir
        Write-Host "  FOUND: $dir" -ForegroundColor Green
        if (-not $pythonExe) { 
            $pythonExe = $candidate
            $pythonDir = $dir
        }
    }
}

# If not found in common locations, try `where.exe` as fallback
if (-not $pythonExe) {
    Write-Host "  Not found in common locations, trying 'where python'..." -ForegroundColor Yellow
    try {
        $whereResult = & where.exe python 2>&1
        if ($LASTEXITCODE -eq 0 -and $whereResult) {
            $firstMatch = ($whereResult -split "`n")[0].Trim()
            if (Test-Path $firstMatch) {
                $pythonExe = $firstMatch
                $pythonDir = Split-Path $firstMatch -Parent
                Write-Host "  FOUND via where.exe: $pythonDir" -ForegroundColor Green
            }
        }
    } catch {
        Write-Host "  where.exe failed: $_" -ForegroundColor Red
    }
}

# Last resort: search the entire LOCALAPPDATA tree for python.exe
if (-not $pythonExe) {
    Write-Host "  Searching %LOCALAPPDATA% for python.exe (this may take a moment)..." -ForegroundColor Yellow
    $found = Get-ChildItem -Path $env:LOCALAPPDATA -Recurse -Filter "python.exe" -ErrorAction SilentlyContinue -Depth 5 |
        Select-Object -First 5
    foreach ($f in $found) {
        $possiblePaths += $f.DirectoryName
        Write-Host "  FOUND: $($f.FullName)" -ForegroundColor Green
        if (-not $pythonExe) {
            $pythonExe = $f.FullName
            $pythonDir = $f.DirectoryName
        }
    }
}

if (-not $pythonExe) {
    Write-Host "ERROR: Could not find python.exe anywhere!" -ForegroundColor Red
    Write-Host "Please install Python 3.14 from https://www.python.org/downloads/" -ForegroundColor Red
    Write-Host "Make sure to check 'Add Python to PATH' during installation." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  Using Python at: $pythonDir" -ForegroundColor Green
$scriptsDir = Join-Path $pythonDir "Scripts"

# Verify Scripts directory exists
if (-not (Test-Path $scriptsDir)) {
    Write-Host "  WARNING: Scripts directory not found at $scriptsDir" -ForegroundColor Yellow
    # Try common alternative: PythonXY\Scripts (sibling to python.exe's dir)
    $parentScripts = Join-Path (Split-Path $pythonDir -Parent) "Scripts"
    if (Test-Path $parentScripts) {
        $scriptsDir = $parentScripts
        Write-Host "  Using alternate Scripts: $scriptsDir" -ForegroundColor Yellow
    }
}

# ──────────────────────────────────────────────────────────────────
# STEP 2: Check current PATH state
# ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[2/5] Checking current PATH..." -ForegroundColor Yellow

$userPath = [Environment]::GetEnvironmentVariable("Path", "User") -split ";"
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine") -split ";"

$pythonInUserPath = $userPath -contains $pythonDir
$scriptsInUserPath = $userPath -contains $scriptsDir
$pythonInMachinePath = $machinePath -contains $pythonDir

Write-Host "  Python dir in User PATH:   $pythonInUserPath"
Write-Host "  Scripts dir in User PATH:  $scriptsInUserPath"
Write-Host "  Python dir in Machine PATH: $pythonInMachinePath"

# Check for Hermes venv shadowing
Write-Host ""
Write-Host "  Checking for Hermes venv shadowing..." -ForegroundColor Yellow
$possibleVenvs = @(
    "$env:USERPROFILE\.hermes",
    "$env:LOCALAPPDATA\hermes",
    "$env:APPDATA\hermes",
    "$env:USERPROFILE\hermes-venv",
    "$env:USERPROFILE\.local\share\hermes"
)
foreach ($venv in $possibleVenvs) {
    if (Test-Path $venv) {
        $venvPython = Join-Path $venv "Scripts\python.exe"
        $venvActivate = Join-Path $venv "Scripts\activate.bat"
        if (Test-Path $venvPython) {
            Write-Host "  FOUND Hermes venv at: $venv" -ForegroundColor Magenta
            Write-Host "    python.exe: $venvPython"
            if (Test-Path $venvActivate) {
                Write-Host "    activate:   $venvActivate"
            }
            # Check if this venv python is shadowing the real one
            $venvDir = Join-Path $venv "Scripts"
            if ($userPath -contains $venvDir) {
                Write-Host "    *** WARNING: This venv is in User PATH and may shadow the real Python! ***" -ForegroundColor Red
                Write-Host "    *** Consider removing it from PATH if you want the system Python by default. ***" -ForegroundColor Red
            }
        }
    }
}

# ──────────────────────────────────────────────────────────────────
# STEP 3: Add Python to persistent User PATH
# ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[3/5] Adding Python to persistent User PATH..." -ForegroundColor Yellow

$currentPath = [Environment]::GetEnvironmentVariable("Path", "User")
$needUpdate = $false

# Build new PATH: existing + pythonDir + scriptsDir (if not already present)
$newEntries = @()
if ($currentPath -notlike "*$pythonDir*") {
    $newEntries += $pythonDir
    $needUpdate = $true
}
if ($currentPath -notlike "*$scriptsDir*") {
    $newEntries += $scriptsDir
    $needUpdate = $true
}

if ($needUpdate) {
    $newPath = $currentPath
    foreach ($entry in $newEntries) {
        $newPath = "$newPath;$entry"
    }
    # Clean up any double semicolons
    $newPath = $newPath -replace ';;+', ';'
    
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    Write-Host "  Added to User PATH:" -ForegroundColor Green
    foreach ($entry in $newEntries) {
        Write-Host "    $entry" -ForegroundColor Green
    }
    
    # Refresh current session's PATH so we can verify
    $env:Path = "$env:Path;$pythonDir;$scriptsDir"
} else {
    Write-Host "  Python is already in PATH — no changes needed." -ForegroundColor Green
    # Still refresh current session
    if ($env:Path -notlike "*$pythonDir*") {
        $env:Path = "$env:Path;$pythonDir;$scriptsDir"
    }
}

# ──────────────────────────────────────────────────────────────────
# STEP 4: Verify the fix
# ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[4/5] Verifying the fix..." -ForegroundColor Yellow

$allOk = $true

# Test python
Write-Host "  Testing: python --version"
try {
    $pyVer = & "$pythonExe" --version 2>&1
    Write-Host "    $pyVer" -ForegroundColor Green
} catch {
    Write-Host "    FAILED: $_" -ForegroundColor Red
    $allOk = $false
}

# Test pip
$pipExe = Join-Path $scriptsDir "pip.exe"
if (Test-Path $pipExe) {
    Write-Host "  Testing: pip --version"
    try {
        $pipVer = & "$pipExe" --version 2>&1
        Write-Host "    $pipVer" -ForegroundColor Green
    } catch {
        Write-Host "    FAILED: $_" -ForegroundColor Red
        $allOk = $false
    }
} else {
    Write-Host "  pip.exe not found at $pipExe" -ForegroundColor Red
    Write-Host "  Trying: python -m pip --version"
    try {
        $pipVer = & "$pythonExe" -m pip --version 2>&1
        Write-Host "    $pipVer" -ForegroundColor Green
    } catch {
        Write-Host "    FAILED: $_" -ForegroundColor Red
        $allOk = $false
    }
}

# Test pyinstaller
$pyinstallerExe = Join-Path $scriptsDir "pyinstaller.exe"
if (Test-Path $pyinstallerExe) {
    Write-Host "  Testing: pyinstaller --version"
    try {
        $pyiVer = & "$pyinstallerExe" --version 2>&1
        Write-Host "    $pyiVer" -ForegroundColor Green
    } catch {
        Write-Host "    FAILED: $_" -ForegroundColor Red
        $allOk = $false
    }
} else {
    Write-Host "  pyinstaller.exe not found at $pyinstallerExe" -ForegroundColor Yellow
    Write-Host "  Install it with: pip install pyinstaller" -ForegroundColor Yellow
    Write-Host "  Or test with: python -m PyInstaller --version"
    try {
        $pyiVer = & "$pythonExe" -m PyInstaller --version 2>&1
        Write-Host "    $pyiVer" -ForegroundColor Green
    } catch {
        Write-Host "    pyinstaller is NOT installed. Run: pip install pyinstaller" -ForegroundColor Yellow
    }
}

# Verify `python` resolves from current session PATH
Write-Host "  Testing: python (via PATH resolution)"
try {
    $cmdResult = cmd /c "python --version 2>&1"
    Write-Host "    $cmdResult" -ForegroundColor Green
} catch {
    Write-Host "    FAILED (may need new terminal): $_" -ForegroundColor Yellow
}

# ──────────────────────────────────────────────────────────────────
# STEP 5: Summary
# ──────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Python location:     $pythonDir" -ForegroundColor White
Write-Host "Scripts location:    $scriptsDir" -ForegroundColor White
Write-Host "Added to User PATH:  $($newEntries -join ', ')" -ForegroundColor White
Write-Host "Fix verified:        $(if ($allOk) { 'YES' } else { 'PARTIAL - see above' })" -ForegroundColor $(if ($allOk) { 'Green' } else { 'Yellow' })

Write-Host ""
Write-Host "=== Next Steps ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. Open a NEW terminal (cmd or PowerShell) for PATH changes to take effect."
Write-Host "2. Verify with:  python --version"
Write-Host "3. Install pyinstaller if needed:  pip install pyinstaller"
Write-Host "4. Run the hyperwall build:  cd ~\hyperwall && .\build.bat"
Write-Host ""
Write-Host "If Python still isn't found in a new terminal, you may need to:"
Write-Host "  - Log out and back in"
Write-Host "  - Or restart Windows Explorer:  taskkill /f /im explorer.exe && start explorer.exe"
Write-Host ""

# Return status
if ($allOk) {
    exit 0
} else {
    exit 2
}
