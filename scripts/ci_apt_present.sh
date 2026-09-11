#!/usr/bin/env bash
#
# ci_apt_present.sh -- exit 0 only if every named dpkg package is installed.
#
# Every CI job ran `apt-get update && apt-get install` under the host-wide
# `packages` lock (scripts/ci_host_lock.sh), even on a warm self-hosted runner
# where every package was already there. Each job held the lock for a full
# index refresh (4-22 s on nucbox after its apt sources moved to https,
# Fri 11 Sep 2026), so a burst of shards queued behind each other past the
# 150 s wall: PR runs started 08:45Z that day hit it on every nucbox shard.
#
# Callers put this in front of the locked install, same package list:
#   scripts/ci_apt_present.sh foo bar || scripts/ci_host_lock.sh packages sudo env -C / sh -c 'apt-get update && apt-get install -y foo bar'
# A warm host skips apt AND the lock; a cold or partial host installs exactly
# as before. Present means dpkg status "install ok installed", so a
# half-configured, removed or unknown package counts as missing. A host with no
# dpkg-query cannot be measured, and says so rather than guessing.
#
# Regression lines:
#   - if every package is installed and this exits nonzero then warm hosts queue on the lock again
#   - if any package is missing, half-installed or unknown and this exits 0 then a cold host skips its install
#   - if called with no package then it must fail with usage, never report "all installed"
set -euo pipefail

if [ "$#" -eq 0 ]; then
    echo "[ERROR] usage: ci_apt_present.sh <package> [package...]" >&2
    exit 2
fi

if ! command -v dpkg-query > /dev/null; then
    echo "[apt-present] no dpkg-query on this host, cannot measure; installing under the packages lock"
    exit 1
fi

missing=()
for pkg in "$@"; do
    # An unknown package makes dpkg-query exit 1 with a message; that is the
    # "missing" answer, so its status stays empty and the name is recorded.
    status=$(dpkg-query -W -f='${Status}' "$pkg" 2> /dev/null || true)
    [ "$status" = "install ok installed" ] || missing+=("$pkg")
done

if [ "${#missing[@]}" -eq 0 ]; then
    echo "[apt-present] all $# packages installed; skipping apt and the packages lock"
    exit 0
fi
echo "[apt-present] missing: ${missing[*]}; installing under the packages lock"
exit 1
