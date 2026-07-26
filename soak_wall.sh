#!/usr/bin/env bash
# Hyperwall macOS audio-transition soak.
# Captures app telemetry plus host memory/VM/network/power snapshots in a
# timestamped directory. The wall self-terminates after HYPERWALL_SOAK_MINUTES.
set -euo pipefail
cd "$(dirname "$0")"

MINUTES="${1:-60}"
REPORT_ROOT="${HYPERWALL_SOAK_REPORT_ROOT:-$PWD/soak_reports}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
REPORT_DIR="$REPORT_ROOT/$RUN_ID"
mkdir -p "$REPORT_DIR"

export HYPERWALL_STATS=1
export HYPERWALL_PERFTRACE=1
export HYPERWALL_SOAK_MINUTES="$MINUTES"
# 20 seconds / cell keeps the audio transition hot without hammering Emby.
export HYPERWALL_SOAK_DWELL_S="${HYPERWALL_SOAK_DWELL_S:-20}"
export HYPERWALL_SOAK_ACTIONS=1
export HYPERWALL_SOAK_PROFILE=audio
export HYPERWALL_SOAK_REPORT_DIR="$REPORT_DIR"

cleanup() {
  local status=$?
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait "${PIDS[@]:-}" 2>/dev/null || true
  printf 'ended_at=%s\nexit_status=%s\n' "$(date -Iseconds)" "$status" >> "$REPORT_DIR/run.env"
}
trap cleanup EXIT INT TERM
PIDS=()

{
  printf 'started_at=%s\n' "$(date -Iseconds)"
  printf 'host='; sw_vers
  printf 'hardware='; system_profiler SPHardwareDataType 2>/dev/null || true
  printf 'env=HYPERWALL_SOAK_MINUTES=%s HYPERWALL_SOAK_DWELL_S=%s HYPERWALL_SOAK_PROFILE=%s\n' \
    "$HYPERWALL_SOAK_MINUTES" "$HYPERWALL_SOAK_DWELL_S" "$HYPERWALL_SOAK_PROFILE"
} > "$REPORT_DIR/run.env"

sample_vm() {
  while :; do
    {
      printf '\n=== %s ===\n' "$(date -Iseconds)"
      vm_stat
      memory_pressure 2>/dev/null || true
    } >> "$REPORT_DIR/vm_stat.log"
    sleep 10
  done
}
sample_net() {
  while :; do
    {
      printf '\n=== %s ===\n' "$(date -Iseconds)"
      nettop -P -L 1 -x -J bytes_in,bytes_out,state 2>/dev/null || true
    } >> "$REPORT_DIR/nettop.log"
    sleep 10
  done
}
sample_power() {
  # powermetrics may request sudo; failure is recorded but never blocks the run.
  powermetrics --samplers tasks,thermal,gpu_power -i 1000 -n "$((MINUTES * 60))" \
    > "$REPORT_DIR/powermetrics.log" 2>&1 || true
}

sample_vm & PIDS+=("$!")
sample_net & PIDS+=("$!")
sample_power & PIDS+=("$!")

printf 'Hyperwall macOS audio soak: %s minutes; artifacts: %s\n' "$MINUTES" "$REPORT_DIR"
./launch.sh 2>&1 | tee "$REPORT_DIR/hyperwall.log"
