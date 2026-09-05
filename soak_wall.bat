@echo off
:: One-hour randomized soak of the wall: full instrumentation on.
:: Launch, click through the wizard as usual (select ALL libraries for
:: coverage), then walk away — the run self-terminates after the hour
:: and dumps stats. The log then holds everything needed for analysis:
::   SOAK res @Nmin      — memory/GDI/USER/thread leak slopes
::   PERF loop-lag/slot  — GUI responsiveness over time
::   [PREFETCH->]/[DIRECT]/stall/error lines — advance + stream health
::   hyperwall_stats_*.json — per-cell decode quality
:: A random cell advances every ~75s on top of natural EOF advances,
:: so one hour churns through hundreds of library items.
setlocal
cd /d "%~dp0"
set HYPERWALL_SOAK_ACTIVE=1
set HYPERWALL_NO_CONFIG_SAVE=1
set HYPERWALL_STATS=1
set HYPERWALL_PERFTRACE=1
set HYPERWALL_SOAK_MINUTES=60
set HYPERWALL_SOAK_DWELL_S=75
call launch.bat
