#!/usr/bin/env bash
# macOS/Linux full-library playback; transcode only after observed failures.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
export HYPERWALL_RENDITION_ROOT="${HYPERWALL_RENDITION_ROOT:-/hyperwall/mv}"
export HYPERWALL_AUDIO_MODE="${HYPERWALL_AUDIO_MODE:-prepared}"
export HYPERWALL_PREPARED_ONLY="${HYPERWALL_PREPARED_ONLY:-0}"
export HYPERWALL_STABLE_DIRECT_ONLY="${HYPERWALL_STABLE_DIRECT_ONLY:-0}"
export HYPERWALL_NORMALIZED_LIBRARY="${HYPERWALL_NORMALIZED_LIBRARY:-0}"
export HYPERWALL_AUTO_TRANSCODE="${HYPERWALL_AUTO_TRANSCODE:-0}"
export HYPERWALL_TRANSCODE_ON_FAILURE="${HYPERWALL_TRANSCODE_ON_FAILURE:-1}"
export HYPERWALL_TRANSCODE_TARGET="${HYPERWALL_TRANSCODE_TARGET:-Hyperwall 1080p}"
export HYPERWALL_LOCAL_TELEMETRY="${HYPERWALL_LOCAL_TELEMETRY:-1}"
export HYPERWALL_STATS="${HYPERWALL_STATS:-1}"
case "$(uname -s)" in
  Darwin) export HYPERWALL_HWDEC="${HYPERWALL_HWDEC:-videotoolbox-copy}" ;;
  Linux) export HYPERWALL_HWDEC="${HYPERWALL_HWDEC:-auto-safe}" ;;
  *) printf '%s\n' 'Use this launcher on macOS or Linux.' >&2; exit 2 ;;
esac
if [ "$HYPERWALL_PREPARED_ONLY" = "1" ]; then
  export HYPERWALL_DEMUXER_PER_CELL_MB="${HYPERWALL_DEMUXER_PER_CELL_MB:-64}"
  export HYPERWALL_CACHE_BUDGET_MB="${HYPERWALL_CACHE_BUDGET_MB:-768}"
fi
exec "$REPO_DIR/launch.sh" "$@"
