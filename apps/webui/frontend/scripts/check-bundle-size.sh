#!/usr/bin/env bash
# scripts/check-bundle-size.sh -- enforce the per-surface client bundle budgets.
#
# Runs after `pnpm build`. Thin wrapper so the CI invocation stays
# `bash scripts/check-bundle-size.sh`; all the measurement lives in
# scripts/bundle-budget.mjs.
#
# One budget per surface (gzip, JS under build/_app/immutable/), one directory
# each under budgets/: limit = budgets/<surface>/base.json plus the sum of
# budgets/<surface>/charges/<pr>.json (budgets/README.md). The byte limits are
# NOT restated here on purpose: figures copied into this header drifted stale
# twice (Tue 1 Sep and Sat 5 Sep 2026). The gate prints them.
# Plus a coverage assertion: an emitted chunk under no budget fails the run.
#
# Usage:
#   bash scripts/check-bundle-size.sh                 # measures ./build
#   bash scripts/check-bundle-size.sh --root DIR      # measures DIR/build
#   bash scripts/check-bundle-size.sh --json          # machine-readable totals
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec node "${HERE}/bundle-budget.mjs" "$@"
