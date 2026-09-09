#!/usr/bin/env bash
# Kept as the explicit name for what `bench_run.sh` already does: run one
# candidate plus the lane's controls and append the numbered round.
# Usage: scripts/bench_post.sh <lane> <candidate> [version]
set -euo pipefail
exec "$(dirname "$0")/bench_run.sh" "${1:?usage: bench_post.sh <lane> <candidate> [version]}" \
  "${2:?usage: bench_post.sh <lane> <candidate> [version]}" "${3:-v1}" --post
