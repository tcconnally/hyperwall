<#
  check_stale.ps1 — Hyperwall stale-binary detector.

  Compares the last-write time of the source entry point against the built exe.
  Used by launch.bat so an out-of-date exe (source edited, forgot to rebuild)
  is flagged instead of silently launched.

  Exit codes (so callers can branch):
    0 = exe is current (or exe/source missing → nothing to warn about here;
        launch.bat handles the missing-exe case on its own)
    2 = exe is STALE (source is newer than exe) — a warning is printed

  Params let the tests point at fixture files.
#>
[CmdletBinding()]
param(
    [string]$Source = "hyperwall.py",
    [string]$Exe = "hyperwall.exe"
)

if (-not (Test-Path -LiteralPath $Exe)) {
    # No exe to compare — not this script's job to complain.
    exit 0
}
if (-not (Test-Path -LiteralPath $Source)) {
    # No source next to the exe (e.g. a frozen-only deployment) — can't compare.
    exit 0
}

$srcTime = (Get-Item -LiteralPath $Source).LastWriteTimeUtc
$exeTime = (Get-Item -LiteralPath $Exe).LastWriteTimeUtc

if ($srcTime -gt $exeTime) {
    $msg = "hyperwall.exe is STALE: $Source (modified $($srcTime.ToString('u'))) " +
           "is newer than the exe (built $($exeTime.ToString('u'))). " +
           "Rebuild with build.bat to pick up your latest changes."
    Write-Warning $msg
    exit 2
}

exit 0
