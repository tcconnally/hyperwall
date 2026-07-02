<#
  test_check_stale.ps1 — verifies scripts/check_stale.ps1 exit codes.

  No Pester dependency: creates temp fixture files with controlled write times
  and asserts the detector's exit code. Run on the Windows CI job.

  Exit 0 = all cases pass; exit 1 = a failure.
#>
$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "..\scripts\check_stale.ps1"
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("hwstale_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null

$fails = 0
function Check($name, $expected, $actual) {
    if ($expected -eq $actual) {
        Write-Host "  PASS  $name"
    } else {
        Write-Host "  FAIL  $name (expected exit $expected, got $actual)"
        $script:fails++
    }
}

try {
    $src = Join-Path $tmp "hyperwall.py"
    $exe = Join-Path $tmp "hyperwall.exe"

    # Case 1: exe newer than source → current → exit 0
    Set-Content -LiteralPath $src -Value "src"
    Start-Sleep -Milliseconds 50
    Set-Content -LiteralPath $exe -Value "exe"
    & powershell -NoProfile -ExecutionPolicy Bypass -File $script -Source $src -Exe $exe
    Check "fresh_exe_exit0" 0 $LASTEXITCODE

    # Case 2: source newer than exe → stale → exit 2
    Start-Sleep -Milliseconds 50
    Set-Content -LiteralPath $src -Value "src-edited"
    & powershell -NoProfile -ExecutionPolicy Bypass -File $script -Source $src -Exe $exe
    Check "stale_exe_exit2" 2 $LASTEXITCODE

    # Case 3: exe missing → nothing to warn → exit 0
    Remove-Item -LiteralPath $exe
    & powershell -NoProfile -ExecutionPolicy Bypass -File $script -Source $src -Exe $exe
    Check "missing_exe_exit0" 0 $LASTEXITCODE

    # Case 4: source missing → can't compare → exit 0
    Set-Content -LiteralPath $exe -Value "exe"
    Remove-Item -LiteralPath $src
    & powershell -NoProfile -ExecutionPolicy Bypass -File $script -Source $src -Exe $exe
    Check "missing_src_exit0" 0 $LASTEXITCODE
}
finally {
    Remove-Item -Recurse -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
}

if ($fails -gt 0) {
    Write-Host "`n$fails failed."
    exit 1
}
Write-Host "`nAll stale-detector cases passed."
exit 0
