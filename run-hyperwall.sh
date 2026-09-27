#!/usr/bin/env bash
# macOS/Linux full-library playback with local performance telemetry.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
export HYPERWALL_RENDITION_ROOT="${HYPERWALL_RENDITION_ROOT:-/hyperwall/mv}"
export HYPERWALL_AUDIO_MODE="${HYPERWALL_AUDIO_MODE:-prepared}"
export HYPERWALL_PREPARED_ONLY="${HYPERWALL_PREPARED_ONLY:-0}"
export HYPERWALL_STABLE_DIRECT_ONLY="${HYPERWALL_STABLE_DIRECT_ONLY:-0}"
export HYPERWALL_NORMALIZED_LIBRARY="${HYPERWALL_NORMALIZED_LIBRARY:-0}"
export HYPERWALL_AUTO_TRANSCODE="${HYPERWALL_AUTO_TRANSCODE:-1}"
export HYPERWALL_MAX_DIRECT_FPS="${HYPERWALL_MAX_DIRECT_FPS:-60}"
export HYPERWALL_MAX_DIRECT_BITRATE_MBPS="${HYPERWALL_MAX_DIRECT_BITRATE_MBPS:-8}"
export HYPERWALL_LOCAL_TELEMETRY="${HYPERWALL_LOCAL_TELEMETRY:-1}"
export HYPERWALL_STATS="${HYPERWALL_STATS:-1}"
case "$(uname -s)" in
  Darwin) export HYPERWALL_HWDEC="${HYPERWALL_HWDEC:-videotoolbox-copy}" ;;
  Linux) export HYPERWALL_HWDEC="${HYPERWALL_HWDEC:-auto-safe}" ;;
  *) printf '%s\n' 'Use this launcher on macOS or Linux.' >&2; exit 2 ;;
esac
# Preserve host defaults for mixed original files. The prepared-only launcher
# has a smaller measured cache budget for the fully bounded 4 Mbps corpus.
exec "$REPO_DIR/launch.sh" "$@"
