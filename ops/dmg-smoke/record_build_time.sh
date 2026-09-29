#!/usr/bin/env bash
# The ONE place that computes a `phase=build_total` verdict and appends it to
# ops/logs/ship-dmg.log. Extracted from the justfile `dmg` recipe's
# `_record_build_time` EXIT trap so there is exactly one executable writer:
# the trap now delegates to this script instead of duplicating its logic
# inline, and the dmg-smoke test harness's headless build shim
# (tests/scripts/dmg_smoke_harness.py) calls this SAME file to fabricate a
# build's timing row. A test fixture and a real `just dmg` build now run the
# identical write path, so they cannot drift apart -- closing the gap a
# hand-duplicated or regex-extracted copy of the format string could not
# (PR #4481 review round 2, P1: "factor the writer into an executable
# production path and exercise that instead").
#
# Usage: record_build_time.sh <root> <elapsed_seconds> <rc>
#   <root> must contain ops/build-budget.env (BUILD_TOTAL_SOFT_S/HARD_S) and
#   is where ops/logs/ship-dmg.log is appended -- the real recipe's
#   $BUILD_WORKTREE, or a test fixture's build_root with a copy of the same
#   budget file.
#
# Exit 1 when the budget file is unreadable AND rc=0 (matching the justfile
# trap's own contract: an unrecordable build is NOT a successful one). Exit 0
# otherwise, including when the budget is unreadable but rc!=0 (the build
# already failed; do not also fail the recording step).
set -euo pipefail

_root="$1"
_elapsed="$2"
_rc="$3"
_budget="$_root/ops/build-budget.env"

if [ ! -r "$_budget" ]; then
    echo "[ERROR] $_budget unreadable: build time NOT recorded" >&2
    [ "$_rc" = 0 ] && exit 1
    exit 0
fi
. "$_budget"

_verdict=OK
[ "$_elapsed" -ge "$BUILD_TOTAL_SOFT_S" ] && _verdict=FLAG_SOFT
[ "$_elapsed" -ge "$BUILD_TOTAL_HARD_S" ] && _verdict=FLAG_HARD
[ "$_rc" = 0 ] || _verdict="${_verdict}_FAILED"

mkdir -p "$_root/ops/logs"
printf '%s run=just-dmg mode=direct phase=build_total seconds=%s rc=%s verdict=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$_elapsed" "$_rc" "$_verdict" \
    >> "$_root/ops/logs/ship-dmg.log"
echo "[TIMING] total=${_elapsed}s rc=${_rc} verdict=$_verdict (soft=${BUILD_TOTAL_SOFT_S}s hard=${BUILD_TOTAL_HARD_S}s, includes 2x Apple notarization)"
