#!/usr/bin/env bash
# Hyperwall macOS audio-transition soak.
# Captures app telemetry plus host memory/VM/network/power snapshots in a
# timestamped directory. The wall self-terminates after HYPERWALL_SOAK_MINUTES.
set -euo pipefail
umask 077
cd "$(dirname "$0")"

MINUTES="${1:-60}"
REPORT_ROOT="${HYPERWALL_SOAK_REPORT_ROOT:-$PWD/soak_reports}"
RUN_ID="$(python3 -c 'from datetime import datetime; print(datetime.now().strftime("%Y%m%d_%H%M%S"))')"
REPORT_DIR="${HYPERWALL_SOAK_REPORT_DIR:-$REPORT_ROOT/$RUN_ID}"
mkdir -p "$REPORT_DIR"
chmod 700 "$REPORT_DIR"

export HYPERWALL_STATS=1
export HYPERWALL_PERFTRACE=1
export HYPERWALL_SOAK_ACTIVE=1
export HYPERWALL_SOAK_MINUTES="$MINUTES"
# 20 seconds / cell keeps the audio transition hot without hammering Emby.
export HYPERWALL_SOAK_DWELL_S="${HYPERWALL_SOAK_DWELL_S:-20}"
export HYPERWALL_SOAK_ACTIONS="${HYPERWALL_SOAK_ACTIONS:-1}"
export HYPERWALL_SOAK_PROFILE="${HYPERWALL_SOAK_PROFILE:-audio}"
export HYPERWALL_SOAK_REPORT_DIR="$REPORT_DIR"

cleanup() {
  local status=$?
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait "${PIDS[@]:-}" 2>/dev/null || true
  printf 'ended_at=%s\nexit_status=%s\n' "$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')" "$status" >> "$REPORT_DIR/run.env"
}
trap cleanup EXIT INT TERM
PIDS=()

{
  printf 'started_at=%s\n' "$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
  printf 'host='; sw_vers
  printf 'hardware='; system_profiler SPHardwareDataType 2>/dev/null || true
  printf 'env=HYPERWALL_SOAK_MINUTES=%s HYPERWALL_SOAK_DWELL_S=%s HYPERWALL_SOAK_PROFILE=%s HYPERWALL_HWDEC=%s HYPERWALL_CACHE_BUDGET_MB=%s HYPERWALL_DEMUXER_PER_CELL_MB=%s\n' "$HYPERWALL_SOAK_MINUTES" "$HYPERWALL_SOAK_DWELL_S" "$HYPERWALL_SOAK_PROFILE" "${HYPERWALL_HWDEC:-}" "${HYPERWALL_CACHE_BUDGET_MB:-}" "${HYPERWALL_DEMUXER_PER_CELL_MB:-}"
  printf 'power_evidence_required=AC_power_lid_open_no_sleep\n'
  printf 'power_evidence_artifact=power_sleep.log\n'
} > "$REPORT_DIR/run.env"

sample_vm() {
  while :; do
    {
      printf '\n=== %s ===\n' "$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
      vm_stat
      memory_pressure 2>/dev/null || true
    } >> "$REPORT_DIR/vm_stat.log"
    sleep 10
  done
}
sample_net() {
  while :; do
    {
      printf '\n=== %s ===\n' "$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
      nettop -P -L 1 -x -J bytes_in,bytes_out,state 2>/dev/null || true
    } >> "$REPORT_DIR/nettop.log"
    sleep 10
  done
}
sample_power_state() {
  # These probes are independent of caffeinate. They record AC/battery state,
  # sleep assertions, and the lid signal so a coordinated sampler gap is not
  # misclassified as a player/cache result.
  while :; do
    {
      printf '\n=== %s ===\n' "$(python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
      if command -v pmset >/dev/null 2>&1; then
        printf '%s\n' '--- pmset -g ps ---'
        pmset -g ps 2>&1 || true
        printf '%s\n' '--- pmset -g assertions ---'
        pmset -g assertions 2>&1 || true
      else
        printf '%s\n' 'pmset=missing'
      fi
      if command -v ioreg >/dev/null 2>&1; then
        printf '%s\n' '--- ioreg AppleClamshellState ---'
        ioreg -n IOPMrootDomain -r -k AppleClamshellState 2>&1 || true
        printf '%s\n' '--- ioreg BatteryData ---'
        ioreg -n IOPMrootDomain -r -k BatteryData 2>&1 || true
      else
        printf '%s\n' 'ioreg=missing'
      fi
    } >> "$REPORT_DIR/power_sleep.log"
    sleep 5
  done
}
sample_power() {
  # powermetrics may request sudo; failure is recorded but never blocks the run.
  powermetrics --samplers tasks,thermal,gpu_power -i 1000 -n "$((MINUTES * 60))" \
    > "$REPORT_DIR/powermetrics.log" 2>&1 || true
}

sample_vm & PIDS+=("$!")
sample_net & PIDS+=("$!")
sample_power_state & PIDS+=("$!")
sample_power & PIDS+=("$!")

printf 'Hyperwall macOS audio soak: %s minutes; artifacts: %s\n' "$MINUTES" "$REPORT_DIR"
if command -v caffeinate >/dev/null 2>&1; then
  # Keep wall-clock duration, monotonic soak duration, and host samplers on the
  # same measurement interval. A 2026-08-25 M5 run lost ~14m45s when idle
  # sleep suspended the app, vm_stat, nettop, and power sampling together.
  caffeinate -dims ./launch.sh 2>&1 | tee "$REPORT_DIR/hyperwall.log"
else
  ./launch.sh 2>&1 | tee "$REPORT_DIR/hyperwall.log"
fi
