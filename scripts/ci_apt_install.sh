#!/usr/bin/env bash
#
# ci_apt_install.sh -- install apt packages as root, the one way CI does it.
#
# The fleet's CI hosts (agentbox, agbox2, agbox3) give the runner user sudo for
# a short list of root-owned helpers only (fleet-af #742), never raw apt-get:
# apt options such as `-o APT::Update::Pre-Invoke=...` run any command as root.
# There the install goes through /usr/local/sbin/ci-apt-install, which refuses
# any name not on its root-owned allowlist; a job that needs a new package fails
# naming it, and the fix is a fleet-af change to
# ci--hosts/ansible/roles/security/files/ci-apt-allowlist.
# A runner without that helper (GitHub-hosted) has unrestricted sudo and runs
# apt-get directly. Both run from / so a root process never holds the workspace
# as its cwd (agentbox, Sun 6 Sep 2026).
#
# Callers keep the presence check and the host lock in front, same packages:
#   scripts/ci_apt_present.sh foo bar || scripts/ci_host_lock.sh packages scripts/ci_apt_install.sh foo bar
#
# Regression lines:
#   - if the helper is installed and this runs apt-get some other way then scoped sudo refuses the job
#   - if called with no package then it must fail with usage, never run apt-get
#   - if a package argument starts with '-' then it must fail before any sudo
set -euo pipefail

HELPER=/usr/local/sbin/ci-apt-install

if [ "$#" -eq 0 ]; then
    echo "[ERROR] usage: ci_apt_install.sh <package> [package...]" >&2
    exit 2
fi
for pkg in "$@"; do
    case "$pkg" in
        -*)
            echo "[ERROR] ci_apt_install.sh: not a package name: $pkg" >&2
            exit 2
            ;;
    esac
done

if [ -x "$HELPER" ]; then
    exec sudo -n "$HELPER" "$@"
fi
exec sudo env -C / sh -c 'apt-get update && apt-get install -y "$@"' sh "$@"
