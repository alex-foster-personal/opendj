#!/usr/bin/env bash
#
# ci_host_lock.sh -- run one command holding a NAMED host-wide lock.
#
# Hosted runners get a fresh VM per job, so two jobs never share anything and
# every workflow here is free to grab a machine-wide resource inline. Self
# hosted runners break that assumption in the same way twice: agentbox runs two
# runner services on one machine, ci.yml and e2e.yml have independent
# `concurrency` groups, so their jobs genuinely overlap on ordinary PR runs.
#
# Two resources on this host are singular, and both fail the same ugly way -- a
# red required check caused by SCHEDULING rather than by the change under test:
#
#   packages          dpkg's own lock does not queue. The loser exits non-zero
#                     with "Could not get lock /var/lib/dpkg/lock-frontend".
#                     `playwright install --with-deps` shells out to apt too.
#   webkit-deckload   tests/e2e/playwright.webkit-deckload.config.ts binds a
#                     FIXED loopback port (8690) with reuseExistingServer
#                     false, deliberately, so two concurrent runs collide. On
#                     one machine per job that is a good design; on a shared
#                     host the second binder just dies.
#
# So take a lock of our own, ahead of the resource, and make the loser WAIT.
# The wait is bounded: a lock held past the timeout is a stuck job, not a busy
# one, and flock exits non-zero so the step says so.
#
# Usage:
#   scripts/ci_host_lock.sh packages sudo sh -c 'apt-get update && apt-get install -y foo'
#   scripts/ci_host_lock.sh webkit-deckload pnpm exec playwright test --config ...
#
# Regression lines:
#   - if two jobs on one host install packages at once and either fails on a
#     dpkg lock then broken
#   - if two jobs on one host run the webkit-deckload suite at once and either
#     fails to bind 8690 then broken
#   - if a lock name is not given then the run must fail rather than share one
#     global lock between unrelated resources
set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "[ERROR] usage: ci_host_lock.sh <lock-name> <command> [args...]" >&2
    exit 2
fi

readonly LOCK_NAME="$1"
shift

# /var/lock is 1777, so any runner user can create this, and flock opens it
# read only, so a file left behind by a different user is still usable.
readonly LOCK_PATH="${MDT_CI_HOST_LOCK_DIR:-/var/lock}/mdt-ci-${LOCK_NAME}.lock"
readonly TIMEOUT_S="${MDT_CI_HOST_LOCK_TIMEOUT_S:-900}"

# Acquire on an inherited descriptor rather than `exec flock ... cmd`, so the
# wait is MEASURED and printed: a suite that spends 200 s behind another job's
# copy of itself looks exactly like a slow suite in the step timing otherwise,
# and the CI lane needs the two apart to know whether to add runners or move
# the suite onto lane ports. The lock stays held by fd 9 for as long as the
# command runs and is released when it exits. Read-only open, so a lock file
# left behind by a different user is still usable.
[ -e "$LOCK_PATH" ] || : > "$LOCK_PATH" 2>/dev/null || true
exec 9< "$LOCK_PATH"
# Non-blocking first: an uncontended lock reports exactly 0, not a wall-clock
# second boundary that happened to fall inside the syscall. Only a contended
# lock is timed, and then in milliseconds.
if flock --nonblock 9; then
    waited_s="0"
else
    waited_from_ms=$(( $(date +%s%N) / 1000000 ))
    if ! flock --timeout "$TIMEOUT_S" 9; then
        echo "[ERROR] host lock '$LOCK_NAME' not acquired within ${TIMEOUT_S}s ($LOCK_PATH); the holder is a stuck job, not a busy one" >&2
        exit 1
    fi
    waited_ms=$(( $(date +%s%N) / 1000000 - waited_from_ms ))
    waited_s="$(( waited_ms / 1000 )).$(printf '%03d' $(( waited_ms % 1000 )))"
fi
echo "[host-lock] $LOCK_NAME acquired after ${waited_s}s"
exec "$@"
