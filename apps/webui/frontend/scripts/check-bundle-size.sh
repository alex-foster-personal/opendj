#!/usr/bin/env bash
# scripts/check-bundle-size.sh -- enforce the per-surface client bundle budgets.
#
# Runs after `pnpm build`. Thin wrapper so the CI invocation stays
# `bash scripts/check-bundle-size.sh`; all the measurement lives in
# scripts/bundle-budget.mjs, which documents each budget and its derivation.
#
# Budgets enforced (gzip, JS under build/_app/immutable/):
#   library      256000  initial load of "/"          (UNCHANGED figure)
#   performance  203776  /performance and children
#   other-lazy    63488  every other route plus deferred shell chunks
# Plus a coverage assertion: an emitted chunk under no budget fails the run.
#
# Usage:
#   bash scripts/check-bundle-size.sh                 # measures ./build
#   bash scripts/check-bundle-size.sh --root DIR      # measures DIR/build
#   bash scripts/check-bundle-size.sh --json          # machine-readable totals
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec node "${HERE}/bundle-budget.mjs" "$@"
