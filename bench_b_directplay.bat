@echo off
:: Benchmark run B — auto-transcode OFF: every item direct-plays and the
:: 5070 Ti does all decode locally (no server-side Emby transcode).
:: Stats harness on. Run the same 5-10 minutes as run A, then press Esc.
setlocal
cd /d "%~dp0"
set HYPERWALL_STATS=1
set HYPERWALL_AUTO_TRANSCODE=0
call launch.bat
