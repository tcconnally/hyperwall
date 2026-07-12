<#
  check_stale.ps1 — Hyperwall stale-binary detector.

  Compares the built exe's last-write time against the NEWEST source file:
  the entry stub AND every .py under the package directory. The stub alone is
  not enough — hyperwall.py is a 6-line shim that almost never changes, so a
  stub-only comparison silently launches a stale exe after edits to
  hyperwall/cell.py etc. (exactly the failure that shipped a v9 binary against
  v10 sources on 2026-07-11).

  Exit codes (so callers can branch):
    0 = exe is current (or exe/source missing → nothing to warn about here;
        launch.bat handles the missing-exe case on its own)
    2 = exe is STALE (source is newer than exe) — a warning is printed

  Params let the tests point at fixture files. -SourceDir defaults to the
  'hyperwall' package directory next to $Source; if it doesn't exist the
  comparison falls back to the stub only (old behavior).
#>
[CmdletBinding()]
param(
    [string]$Source = "hyperwall.py",
    [string]$Exe = "hyperwall.exe",
    [string]$SourceDir = ""
)

if (-not $SourceDir) {
    $srcParent = Split-Path -Parent ([System.IO.Path]::GetFullPath($Source))
    $SourceDir = Join-Path $srcParent "hyperwall"
}

if (-not (Test-Path -LiteralPath $Exe)) {
    # No exe to compare — not this script's job to complain.
    exit 0
}
if (-not (Test-Path -LiteralPath $Source)) {
    # No source next to the exe (e.g. a frozen-only deployment) — can't compare.
    exit 0
}

$newestTime = (Get-Item -LiteralPath $Source).LastWriteTimeUtc
$newestFile = $Source

if ($SourceDir -and (Test-Path -LiteralPath $SourceDir)) {
    $pkgNewest = Get-ChildItem -LiteralPath $SourceDir -Recurse -Filter *.py |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if ($pkgNewest -and $pkgNewest.LastWriteTimeUtc -gt $newestTime) {
        $newestTime = $pkgNewest.LastWriteTimeUtc
        $newestFile = $pkgNewest.FullName
    }
}

$exeTime = (Get-Item -LiteralPath $Exe).LastWriteTimeUtc

if ($newestTime -gt $exeTime) {
    $msg = "hyperwall.exe is STALE: $newestFile (modified $($newestTime.ToString('u'))) " +
           "is newer than the exe (built $($exeTime.ToString('u'))). " +
           "Rebuild with build.bat to pick up your latest changes."
    Write-Warning $msg
    exit 2
}

exit 0
