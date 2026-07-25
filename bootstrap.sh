#!/usr/bin/env bash
# HyperWall — one-shot macOS bootstrap (Apple Silicon + Intel)
# Usage: ./bootstrap.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==================== HYPERWALL macOS BOOTSTRAP ===================="
echo ""

# ── 1. Homebrew ──────────────────────────────────────────────────────
if ! command -v brew >/dev/null 2>&1; then
    echo "[FAIL] Homebrew not found. Install from https://brew.sh first:"
    echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    exit 1
fi
echo "[OK] Homebrew: $(brew --version | head -1)"

# ── 2. libmpv (the mpv formula ships libmpv.dylib) ──────────────────
BREW_PREFIX="$(brew --prefix)"
if ! ls "$BREW_PREFIX"/lib/libmpv*.dylib >/dev/null 2>&1; then
    echo "[*] Installing mpv (provides libmpv)..."
    brew install mpv
fi
echo "[OK] libmpv: $(ls "$BREW_PREFIX"/lib/libmpv*.dylib | head -1)"

# ── 3. Python 3.12+ ──────────────────────────────────────────────────
PY="python3"
if ! $PY -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    echo "[*] python3 missing or < 3.12 — installing python@3.13..."
    brew install python@3.13
    PY="$BREW_PREFIX/bin/python3.13"
fi
echo "[OK] Python: $($PY --version 2>&1)"

# ── 4. venv + Python deps ────────────────────────────────────────────
if [ ! -d .venv ]; then
    echo "[*] Creating .venv..."
    "$PY" -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet python-mpv pyqt6 requests flask
echo "[OK] Python deps installed (python-mpv, pyqt6, requests, flask)"

# ── 5. config ────────────────────────────────────────────────────────
if [ ! -f config.ini ]; then
    cp config.example.ini config.ini
    echo "[*] config.ini created — edit server_url / username / password."
else
    echo "[OK] config.ini present"
fi

# ── 6. verify libmpv binding (same env as launch.sh) ─────────────────
export DYLD_FALLBACK_LIBRARY_PATH="$BREW_PREFIX/lib:/usr/local/lib${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
export LC_NUMERIC=C  # libmpv's check_locale rejects non-C LC_NUMERIC
if ./.venv/bin/python -c "import mpv" 2>/tmp/hyperwall_mpv_err; then
    echo "[OK] python-mpv bound to libmpv"
else
    echo "[FAIL] python-mpv could not load libmpv:"
    cat /tmp/hyperwall_mpv_err
    echo "  Check that 'brew install mpv' succeeded and re-run ./bootstrap.sh"
    exit 1
fi

# Headless probe: actually create a player (catches locale/ABI failures
# that a bare import can't — mpv_create() returning NULL segfaults
# python-mpv at first use, so verify it HERE, not at first playback).
if ./.venv/bin/python - <<'EOF' 2>/tmp/hyperwall_mpv_err
import mpv
# Creation alone is the probe (mpv_create NULL → python-mpv segfault).
# Don't query properties here: names differ across mpv versions.
m = mpv.MPV(vo="null", vid="no", aid="no", idle="yes")
m.terminate()
print("[OK] mpv player created and terminated cleanly")
EOF
then
    :
else
    echo "[FAIL] libmpv loaded but mpv_create() failed:"
    cat /tmp/hyperwall_mpv_err
    exit 1
fi

echo ""
echo "==================== DONE ===================="
echo "  Configure: edit config.ini (Emby server_url/username/password)"
echo "  Launch:    ./launch.sh"
