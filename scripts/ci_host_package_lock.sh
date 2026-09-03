#!/usr/bin/env bash
#
# ci_host_package_lock.sh -- run one command holding the host's package lock.
#
# Hosted runners get a fresh VM per job, so two jobs never touch one dpkg
# database and every workflow here installs its own apt packages inline. Self
# hosted runners break that assumption: agentbox runs two runner services on one
# machine, ci.yml and e2e.yml have independent `concurrency` groups, and their
# apt steps (plus `playwright install --with-deps`, which shells out to apt
# itself) therefore overlap on ordinary PR runs. dpkg's own lock does not
# queue -- the loser exits non-zero with "Could not get lock
# /var/lib/dpkg/lock-frontend", which is a red required check caused by
# scheduling rather than by the change under test.
#
# So take a lock of our own, ahead of apt, and make the loser WAIT instead.
# The wait is bounded: a lock held past the timeout is a stuck job, not a busy
# one, and flock exits non-zero so the step says so.
#
# Usage:
#   scripts/ci_host_package_lock.sh sudo sh -c 'apt-get update && apt-get install -y foo'
#   scripts/ci_host_package_lock.sh pnpm exec playwright install --with-deps webkit
#
# Regression line:
#   - if two jobs on one host install packages at once and either fails on a
#     dpkg lock then broken
set -euo pipefail

# /var/lock is 1777, so any runner user can create this, and flock opens it
# read only, so a file left behind by a different user is still usable.
readonly LOCK_PATH="${MDT_CI_HOST_LOCK:-/var/lock/mdt-ci-host-packages.lock}"
readonly TIMEOUT_S="${MDT_CI_HOST_LOCK_TIMEOUT_S:-900}"

if [ "$#" -eq 0 ]; then
    echo "[ERROR] ci_host_package_lock.sh needs a command to run" >&2
    exit 2
fi

exec flock --timeout "$TIMEOUT_S" "$LOCK_PATH" "$@"
