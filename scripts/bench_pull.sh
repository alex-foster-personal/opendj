#!/usr/bin/env bash
# Pull a fixture bundle by checksum. Usage: scripts/bench_pull.sh <lane> <version>
# Refuses, and leaves nothing on disk, when the download does not match.
set -euo pipefail
LANE="${1:?usage: bench_pull.sh <lane> <version> [bundle-id]}"
VERSION="${2:?usage: bench_pull.sh <lane> <version> [bundle-id]}"
EXPECT="${3:-}"
cd "$(dirname "$0")/.."
ARGS=(fixtures pull --lane "$LANE" --version "$VERSION")
[ -n "$EXPECT" ] && ARGS+=(--expect-bundle-id "$EXPECT")
exec uv run --no-sync python -m apps.analysis_bench "${ARGS[@]}"
