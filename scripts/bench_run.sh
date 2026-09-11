#!/usr/bin/env bash
# The NATIVE-11 entry point: pull the bundle if this clone does not hold it,
# run one candidate plus the lane's controls, score, and APPEND the numbered
# round to the lane's experiment log.
#
# From a fresh clone this is the whole contract, because `data/` is untracked:
# the bundle has to arrive by checksum before anything can be measured, and a
# run nobody logged is a measurement a later session cannot resume from.
#
# Usage: scripts/bench_run.sh <lane> <candidate> [version] [--no-post]
# The round block is a spec edit: commit it on your branch, do not push to main.
set -euo pipefail
LANE="${1:?usage: bench_run.sh <lane> <candidate> [version] [--no-post]}"
CANDIDATE="${2:?usage: bench_run.sh <lane> <candidate> [version] [--no-post]}"
VERSION="${3:-v1}"
POST="${4:---post}"
case "$POST" in
  --post|--no-post) ;;
  *) echo "[bench] fourth argument is --post or --no-post, got '$POST'" >&2; exit 2 ;;
esac
cd "$(dirname "$0")/.."

BUNDLE="data/bench/${LANE}/${VERSION}"
if [ ! -f "${BUNDLE}/BUNDLE_ID" ]; then
  echo "[bench] ${BUNDLE} is not in this clone; pulling it by checksum first"
  ./scripts/bench_pull.sh "$LANE" "$VERSION"
fi

ARGS=(run --lane "$LANE" --candidate "$CANDIDATE" --version "$VERSION"
      --out ".tmp/analysis_bench/${LANE}-${CANDIDATE}-report.json")
[ "$POST" = "--post" ] && ARGS+=(--post)
exec uv run --no-sync python -m apps.analysis_bench "${ARGS[@]}"
