@echo off
:: Benchmark run A — current defaults (auto-transcode >1080p on the server),
:: with the mpv stats harness on. Use the wall normally for 5-10 minutes,
:: then press Esc: a hyperwall_stats_*.json dump appears next to the exe.
setlocal
cd /d "%~dp0"
set HYPERWALL_STATS=1
call launch.bat
