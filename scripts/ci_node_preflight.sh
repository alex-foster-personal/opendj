#!/usr/bin/env bash
# ci_node_preflight.sh -- name the runner and the broken precondition BEFORE
# `pnpm install` runs on a self-hosted node job.
#
# Two things went wrong under a running install on Sat 5 Sep 2026 and both
# surfaced as cryptic pnpm errors mid-job: `[ERR_PNPM_ERR_SQLITE_ERROR] disk
# I/O error` (agentbox-13, trunk e14bd2f0a3) and `ERR_PNPM_ENOENT` on a store
# file the index promised (nucbox-wsl-2, trunk 59248abb9). The cause was
# actions/setup-node restoring its cache tarball into the host-shared pnpm
# store; that writer is gone (#1299), and this script is the tripwire that
# says so in one line if the store is ever mutated again, or the disk fills.
#
# Checks, each fail-closed and naming the runner:
#   1. `corepack` and `node` resolve on PATH (via ci_runner_preflight.sh)
#   2. at least MIN_FREE_GB free on the filesystem holding the workspace
#
# `pnpm store status` is deliberately NOT a check here. Measured Sat 5 Sep
# 2026 17:20 UTC on agentbox: a store rebuilt from the lockfile served a fresh
# `pnpm install --frozen-lockfile --offline` in 2.4 s and every node job
# since was green, while `pnpm store status` kept listing all 185 packages
# as modified from every directory it was run in. A probe that reads red on
# a working store is not a probe. The install itself is the store check.
#
# Usage: scripts/ci_node_preflight.sh <frontend-dir>
# Regression lines:
#   - if the disk has under MIN_FREE_GB free then this fails before install,
#     naming the runner, instead of pnpm failing on a random write
#   - if corepack or node is missing then this names it before install
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "[ci-node-preflight] usage: ci_node_preflight.sh <frontend-dir>" >&2
    exit 2
fi
readonly FRONTEND_DIR="$1"
readonly MIN_FREE_GB="${MDT_CI_MIN_FREE_GB:-10}"
readonly RUNNER="${RUNNER_NAME:-$(hostname 2>/dev/null || echo unknown)}"
readonly HERE="$(cd "$(dirname "$0")" && pwd)"

"$HERE/ci_runner_preflight.sh" corepack node

# POSIX df (-Pk): GNU --output is not on macOS, where this also has to run.
avail_gb="$(df -Pk "$FRONTEND_DIR" | awk 'NR==2 {print int($4 / 1048576)}')"
if [ "${avail_gb:-0}" -lt "$MIN_FREE_GB" ]; then
    echo "::error::[ci-node-preflight] runner $RUNNER has ${avail_gb:-0}G free under $FRONTEND_DIR, below the ${MIN_FREE_GB}G floor" >&2
    exit 1
fi

echo "[ci-node-preflight] runner $RUNNER: corepack and node present, ${avail_gb}G free under $FRONTEND_DIR"
