@echo off
:: Launch the wall with the GUI-responsiveness tracer on.
:: Use the wall normally; when something feels sluggish, note roughly when.
:: The log gets "PERF loop-lag" summaries every 10s, "PERF loop stall"
:: warnings for main-thread blocks >100ms, and "PERF slow slot" lines for
:: any interaction handler that took >25ms.
setlocal
cd /d "%~dp0"
set HYPERWALL_PERFTRACE=1
call launch.bat
