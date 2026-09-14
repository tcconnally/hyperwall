#!/usr/bin/env bash
# HyperWall — Pop!_OS/NVIDIA production launcher.
#
# The production library is pre-normalized to MP4/H.264/AAC before Emby sees
# it. Runtime HLS transcoding is intentionally disabled: an unnormalized item
# is excluded and reported instead of entering the eight-cell wall.
set -euo pipefail

# Soak-only variables must not leak into an everyday production launch.
if [ "${HYPERWALL_SOAK_ACTIVE:-0}" != "1" ]; then
  unset HYPERWALL_SOAK_MINUTES
  unset HYPERWALL_SOAK_DWELL_S
  unset HYPERWALL_SOAK_ACTIONS
  unset HYPERWALL_SOAK_PROFILE
  unset HYPERWALL_SOAK_FILTER
  unset HYPERWALL_SOAK_ITEM_ID
  unset HYPERWALL_SOAK_REPORT_DIR
  unset HYPERWALL_SOAK_REPORT_ROOT
  unset HYPERWALL_NO_RELAUNCH
  unset HYPERWALL_NO_LOG_SETUP
fi

cd "$(dirname "$0")"

# Production defaults can be overridden explicitly for a diagnostic run.
export HYPERWALL_NORMALIZED_LIBRARY="${HYPERWALL_NORMALIZED_LIBRARY:-1}"
export HYPERWALL_AUTO_TRANSCODE="${HYPERWALL_AUTO_TRANSCODE:-0}"
export HYPERWALL_UNLOAD_OLLAMA="${HYPERWALL_UNLOAD_OLLAMA:-1}"
export HYPERWALL_HARDWARE_PREFLIGHT="${HYPERWALL_HARDWARE_PREFLIGHT:-1}"
export HYPERWALL_OLLAMA_URL="${HYPERWALL_OLLAMA_URL:-http://127.0.0.1:11434}"
export LC_NUMERIC=C

case "$HYPERWALL_UNLOAD_OLLAMA" in
  0|1) ;;
  *)
    printf '%s\n' 'HYPERWALL_UNLOAD_OLLAMA must be 0 or 1' >&2
    exit 2
    ;;
esac

case "$HYPERWALL_HARDWARE_PREFLIGHT" in
  0|1) ;;
  *)
    printf '%s\n' 'HYPERWALL_HARDWARE_PREFLIGHT must be 0 or 1' >&2
    exit 2
    ;;
esac

case "$HYPERWALL_NORMALIZED_LIBRARY" in
  0|1) ;;
  *)
    printf '%s\n' 'HYPERWALL_NORMALIZED_LIBRARY must be 0 or 1' >&2
    exit 2
    ;;
esac

case "$HYPERWALL_AUTO_TRANSCODE" in
  0|1) ;;
  *)
    printf '%s\n' 'HYPERWALL_AUTO_TRANSCODE must be 0 or 1' >&2
    exit 2
    ;;
esac

if [ "$HYPERWALL_NORMALIZED_LIBRARY" = "0" ] && [ "$HYPERWALL_AUTO_TRANSCODE" = "0" ]; then
  printf '%s\n' 'cannot disable normalized library while auto-transcode is disabled' >&2
  exit 2
fi

PY="./.venv/bin/python"
[ -x "$PY" ] || PY="python3"

# Fail before GPU/Ollama side effects when the GUI runtime is incomplete.
# The system Python on Pop!_OS often has no pip or python-mpv; use the
# repository-local bootstrap instead of allowing the Qt error path to run.
if ! "$PY" -c 'import mpv, PyQt6, requests, flask' >/dev/null 2>&1; then
  printf '%s\n' '[FAIL] HyperWall Python runtime is incomplete.' >&2
  printf '%s\n' "       Interpreter: $PY" >&2
  printf '%s\n' '       Run ./bootstrap-linux.sh, then rerun ./launch-linux.sh.' >&2
  exit 2
fi

if [ "$HYPERWALL_HARDWARE_PREFLIGHT" = "1" ]; then
  "$PY" scripts/preflight-linux-production.py --required
fi

if [ "$HYPERWALL_UNLOAD_OLLAMA" = "1" ]; then
  "$PY" scripts/unload-ollama.py \
    --url "$HYPERWALL_OLLAMA_URL" \
    --wait-s "${HYPERWALL_OLLAMA_WAIT_S:-20}" \
    --required
fi

exec "$PY" hyperwall.py "$@"
