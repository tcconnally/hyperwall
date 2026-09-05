#!/usr/bin/env bash
# HyperWall — macOS launcher (script mode; the .exe/G-Sync path is Windows-only)
set -euo pipefail

# A benchmark shell may have exported soak-only variables.  Keep the normal
# launcher from accidentally turning an everyday session into a randomized
# self-terminating run; soak_wall.sh and the diagnostic runner set the marker
# explicitly before invoking this script.
if [ "${HYPERWALL_SOAK_ACTIVE:-0}" != "1" ]; then
  unset HYPERWALL_SOAK_MINUTES
  unset HYPERWALL_SOAK_DWELL_S
  unset HYPERWALL_SOAK_ACTIONS
  unset HYPERWALL_SOAK_PROFILE
  unset HYPERWALL_SOAK_FILTER
  unset HYPERWALL_SOAK_REPORT_DIR
  unset HYPERWALL_SOAK_REPORT_ROOT
  unset HYPERWALL_NO_RELAUNCH
  unset HYPERWALL_NO_LOG_SETUP
fi

cd "$(dirname "$0")"

# python-mpv finds libmpv via ctypes.util.find_library, which consults
# DYLD_FALLBACK_LIBRARY_PATH — but only as it was at process start.
# Homebrew's lib dir (/opt/homebrew/lib on Apple Silicon, /usr/local/lib on
# Intel) is not in dyld's default fallback, so export it BEFORE exec'ing
# python. Setting it later from inside Python would be a no-op.
BREW_PREFIX="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
export DYLD_FALLBACK_LIBRARY_PATH="$BREW_PREFIX/lib:/usr/local/lib${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"

# libmpv refuses to create a player under a non-C LC_NUMERIC locale
# (mpv check_locale); Python sets it from the environment at startup.
# app.py also forces it in-process — this is defense-in-depth.
export LC_NUMERIC=C

PY="./.venv/bin/python"
[ -x "$PY" ] || PY="python3"

exec "$PY" hyperwall.py "$@"
