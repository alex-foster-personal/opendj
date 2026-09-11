#!/usr/bin/env bash
# BAD FIXTURE for scripts/shell_construct_lint.py rule `status-after-substitution`.
# Never sourced, never run.
#
# A command substitution RUNS a command, and bash and zsh both update $? when it
# finishes, while the rest of the same command is still being expanded. So any
# $? read after a $(...) or `...` in one simple command reports the
# substitution's status (date: 0), never the command the author meant.
# Measured Fri 11 Sep 2026, bash 3.2.57 and zsh 5.9, `f(){ return 5; }; f; ...`:
# every form below printed 0. Only dash printed 5.
#
# The first instance is the live one from scripts/run_acapella_two_batch.sh,
# which logged rc=0 for every Modal batch whatever it returned.
set -uo pipefail

fire() {
    local rid="$1"; shift
    modal_separate "$@"
    echo "[$(date '+%H:%M:%S')] $rid returned rc=$?"
}

fire_backtick() {
    modal_separate "$@"
    echo "`date +%s` rc=${?}"
}

fire_assignment_pair() {
    modal_separate "$@"
    stamp=$(date +%s) rc=$?
}
