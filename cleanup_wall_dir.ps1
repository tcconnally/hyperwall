# =============================================================================
# HyperWall wall-directory cleanup
#
# Target: C:\Users\tccon\OneDrive\Documents\scripts\wall
# Safe by default: run without -Apply for a dry run. With -Apply, files are
# moved into .\_archive\hyperwall_cleanup_<timestamp>\, never deleted.
# =============================================================================

[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [string]$WallDir = 'C:\Users\tccon\OneDrive\Documents\scripts\wall',
    [switch]$Apply,
    [switch]$KeepLegacyV7
)

$ErrorActionPreference = 'Stop'

function RelPath([string]$Path, [string]$Base) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $fullBase = [System.IO.Path]::GetFullPath($Base)
    return [System.IO.Path]::GetRelativePath($fullBase, $fullPath)
}

function Add-ItemIfExists([System.Collections.Generic.List[string]]$List, [string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $List.Add((Resolve-Path -LiteralPath $Path).Path)
    }
}

if (-not (Test-Path -LiteralPath $WallDir -PathType Container)) {
    throw "WallDir not found: $WallDir"
}

$WallDir = (Resolve-Path -LiteralPath $WallDir).Path
Set-Location -LiteralPath $WallDir

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$archiveRoot = Join-Path $WallDir "_archive\hyperwall_cleanup_$stamp"
$manifest = Join-Path $archiveRoot 'manifest.tsv'

$move = [System.Collections.Generic.List[string]]::new()

# Python/build/runtime detritus.
Get-ChildItem -LiteralPath $WallDir -Directory -Force -Recurse |
    Where-Object {
        $_.FullName -notmatch '\\.git(\\|$)' -and
        $_.FullName -notmatch '\\_archive(\\|$)' -and
        $_.Name -in @('__pycache__', '.pytest_cache', 'build', 'dist')
    } |
    ForEach-Object { $move.Add($_.FullName) }

# Logs, diagnostics, generated test/stress artifacts, temporary specs/backups.
Get-ChildItem -LiteralPath $WallDir -File -Force |
    Where-Object {
        $_.Name -like '*.log' -or
        $_.Name -like 'hyperwall.log*' -or
        $_.Name -like 'hyperwall_stats_*.json' -or
        $_.Name -like 'stress_*.log' -or
        $_.Name -like 'test_*_results.json' -or
        $_.Name -like '*.spec' -or
        $_.Name -like '*.bak' -or
        $_.Name -like '*.bak.*' -or
        $_.Name -eq '.hyperwall_v8_nvprofile.sentinel'
    } |
    ForEach-Object { $move.Add($_.FullName) }

# Legacy v7 monolith and old one-off backups. Kept by request switch.
if (-not $KeepLegacyV7) {
    Add-ItemIfExists $move (Join-Path $WallDir 'hyperwall.py')
    Get-ChildItem -LiteralPath $WallDir -File -Force -Filter 'hyperwall_v8.bak*.py' |
        ForEach-Object { $move.Add($_.FullName) }
}

# De-dupe and never move active v8 essentials.
$activeNames = @(
    '.gitignore',
    'bootstrap_v8.ps1',
    'build_v8.bat',
    'cleanup_wall_dir.ps1',
    'config.ini',
    'config.example.ini',
    'hyperwall_v8.exe',
    'hyperwall_v8.nip',
    'hyperwall_v8.py',
    'launch.bat',
    'mpv-2.dll'
)
$activeDirs = @('hyperwall', 'tools', '.git', '_archive')

$items = $move |
    Sort-Object -Unique |
    Where-Object {
        $name = Split-Path -Leaf $_
        $isActiveName = $activeNames -contains $name
        $isActiveDir = (Test-Path -LiteralPath $_ -PathType Container) -and ($activeDirs -contains $name)
        -not $isActiveName -and -not $isActiveDir
    }

Write-Host "HyperWall cleanup target: $WallDir" -ForegroundColor Cyan
Write-Host "Mode: $(if ($Apply) { 'APPLY - move to archive' } else { 'DRY RUN - no changes' })" -ForegroundColor Yellow
Write-Host "Archive: $archiveRoot"
Write-Host ""

if (-not $items -or $items.Count -eq 0) {
    Write-Host 'Nothing to clean.' -ForegroundColor Green
    exit 0
}

$items | ForEach-Object { Write-Host ("  {0}" -f (RelPath $_ $WallDir)) }
Write-Host ""

if (-not $Apply) {
    Write-Host 'Dry run only. Re-run with -Apply to move these files/directories into the archive.' -ForegroundColor Yellow
    exit 0
}

New-Item -ItemType Directory -Force -Path $archiveRoot | Out-Null
"OriginalPath`tArchivePath" | Set-Content -LiteralPath $manifest -Encoding UTF8

foreach ($item in $items) {
    if (-not (Test-Path -LiteralPath $item)) { continue }
    $rel = RelPath $item $WallDir
    $dest = Join-Path $archiveRoot $rel
    $destParent = Split-Path -Parent $dest
    New-Item -ItemType Directory -Force -Path $destParent | Out-Null
    Move-Item -LiteralPath $item -Destination $dest -Force
    "{0}`t{1}" -f $item, $dest | Add-Content -LiteralPath $manifest -Encoding UTF8
}

Write-Host "Moved $($items.Count) item(s) into:" -ForegroundColor Green
Write-Host "  $archiveRoot"
Write-Host "Manifest:" -ForegroundColor Green
Write-Host "  $manifest"
Write-Host ""
Write-Host 'Active v8 files were left in place. If something is missing, recover it from the archive folder.' -ForegroundColor Cyan
