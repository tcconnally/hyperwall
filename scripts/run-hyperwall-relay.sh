#!/usr/bin/env bash
# HyperWall — headless sync relay for mb.perseus.observer
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

# Prefer the repo venv, fall back to system python3
PY="./.venv/bin/python"
[ -x "$PY" ] || PY="python3"

HOST="${HYPERWALL_SYNC_HOST:-0.0.0.0}"
PORT="${HYPERWALL_SYNC_PORT:-9876}"

exec "$PY" hyperwall.py --sync-relay --sync-host "$HOST" --sync-port "$PORT"
