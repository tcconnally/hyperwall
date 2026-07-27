#!/usr/bin/env bash
# HyperWall — macOS client launcher for Mark (or any macOS peer)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

CONFIG="$REPO_DIR/config.ini"

if [ ! -f "$CONFIG" ]; then
    echo "Missing $CONFIG" >&2
    echo "Run the app once to create it, or copy a prepared config.ini into the repo." >&2
    exit 1
fi

# Quick sanity check that sync is pointed at the relay
if ! grep -qE '^sync_enabled\s*=\s*true' "$CONFIG"; then
    echo "WARNING: sync_enabled is not true in $CONFIG" >&2
fi
if ! grep -qE '^sync_server\s*=\s*false' "$CONFIG"; then
    echo "WARNING: sync_server should be false when connecting to mb.perseus.observer relay" >&2
fi

exec ./launch.sh "$@"
