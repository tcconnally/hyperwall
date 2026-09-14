#!/usr/bin/env bash
# HyperWall — Pop!_OS/Linux runtime bootstrap.
# Creates a user-local virtualenv; no sudo is required when Python venv support
# and network access are already available.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  printf '%s\n' "[FAIL] $PYTHON_BIN not found." >&2
  exit 2
fi

if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  printf '%s\n' '[FAIL] Python 3.12 or newer is required.' >&2
  exit 2
fi

if [ -e .venv ] && [ ! -x .venv/bin/python ]; then
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  printf '%s\n' '[*] Creating .venv...'
  # Some Ubuntu/Pop!_OS installs omit python3-venv/ensurepip. The venv
  # itself still works without pip; the fallback below seeds pip in-user.
  "$PYTHON_BIN" -m venv --without-pip .venv
fi

PY="./.venv/bin/python"
if ! "$PY" -m pip --version >/dev/null 2>&1; then
  PIP_BOOTSTRAP="$(mktemp)"
  trap 'rm -f "$PIP_BOOTSTRAP"' EXIT
  printf '%s\n' '[*] Seeding pip in .venv...'
  "$PYTHON_BIN" -c 'import sys, urllib.request; urllib.request.urlretrieve("https://bootstrap.pypa.io/get-pip.py", sys.argv[1])' "$PIP_BOOTSTRAP"
  "$PY" "$PIP_BOOTSTRAP" --disable-pip-version-check
  rm -f "$PIP_BOOTSTRAP"
  trap - EXIT
fi

if ! "$PY" -m pip --version >/dev/null 2>&1; then
  printf '%s\n' '[FAIL] Could not provision pip inside .venv.' >&2
  exit 2
fi

printf '%s\n' '[*] Installing Python runtime dependencies...'
"$PY" -m pip install --upgrade pip
"$PY" -m pip install --upgrade python-mpv PyQt6 requests flask

printf '%s\n' '[*] Verifying Python imports and libmpv player creation...'
"$PY" - <<'PY'
import mpv
import PyQt6
import flask
import requests

player = mpv.MPV(vo="null", vid="no", aid="no", idle="yes")
player.terminate()
print("[OK] python-mpv, PyQt6, requests, Flask, and libmpv are usable")
PY

printf '\n%s\n' '[OK] Linux runtime ready.'
printf '%s\n' '     Launch with: ./launch-linux.sh'
