#!/usr/bin/env bash
# Start the existing wall with only finalized Emby playback copies.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export HYPERWALL_RENDITION_ROOT="${HYPERWALL_RENDITION_ROOT:-/hyperwall/mv}"
export HYPERWALL_AUDIO_MODE="${HYPERWALL_AUDIO_MODE:-prepared}"
export HYPERWALL_PREPARED_ONLY="${HYPERWALL_PREPARED_ONLY:-1}"
export HYPERWALL_STABLE_DIRECT_ONLY="${HYPERWALL_STABLE_DIRECT_ONLY:-1}"
export HYPERWALL_STABLE_MAX_FPS="${HYPERWALL_STABLE_MAX_FPS:-60}"
export HYPERWALL_STABLE_MAX_BITRATE_MBPS="${HYPERWALL_STABLE_MAX_BITRATE_MBPS:-8}"

if [ "$(uname -s)" = "Darwin" ]; then
    # Verified with twelve prepared 1080p60 streams on the 16 GB M5.
    # Copy mode avoids the render-API hardware-surface interop path.
    export HYPERWALL_HWDEC="${HYPERWALL_HWDEC:-videotoolbox-copy}"
    # 64 MiB holds over two minutes at the 4 Mbps preparation target.
    export HYPERWALL_DEMUXER_PER_CELL_MB="${HYPERWALL_DEMUXER_PER_CELL_MB:-64}"
    export HYPERWALL_CACHE_BUDGET_MB="${HYPERWALL_CACHE_BUDGET_MB:-768}"
fi

exec "$REPO_DIR/launch.sh" "$@"
