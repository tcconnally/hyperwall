#!/usr/bin/env bash
# HyperWall — macOS launcher (script mode; the .exe/G-Sync path is Windows-only)
set -euo pipefail
cd "$(dirname "$0")"

# python-mpv finds libmpv via ctypes.util.find_library, which consults
# DYLD_FALLBACK_LIBRARY_PATH — but only as it was at process start.
# Homebrew's lib dir (/opt/homebrew/lib on Apple Silicon, /usr/local/lib on
# Intel) is not in dyld's default fallback, so export it BEFORE exec'ing
# python. Setting it later from inside Python would be a no-op.
BREW_PREFIX="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
export DYLD_FALLBACK_LIBRARY_PATH="$BREW_PREFIX/lib:/usr/local/lib${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"

PY="./.venv/bin/python"
[ -x "$PY" ] || PY="python3"

exec "$PY" hyperwall.py "$@"
