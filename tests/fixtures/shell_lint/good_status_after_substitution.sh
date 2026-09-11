#!/usr/bin/env bash
# GOOD FIXTURE for rule `status-after-substitution`: every shape here reads the
# status the author meant. Each was measured in bash 3.2.57 and zsh 5.9 on
# Fri 11 Sep 2026 against `f(){ return 5; }; f; ...` and printed 5.
set -uo pipefail

# The fix: capture the status first, then build the message.
fire() {
    local rid="$1"; shift
    modal_separate "$@"
    rc=$?; echo "[$(date '+%H:%M:%S')] $rid returned rc=$rc"
}

# $? read BEFORE the substitution runs is still the previous command's.
status_then_stamp() {
    modal_separate "$@"
    echo "rc=$? at $(date +%s)"
}

# Arithmetic expansion runs no command, so it leaves $? alone.
arithmetic_is_inert() {
    modal_separate "$@"
    echo "$((1 + 1)) rc=$?"
}

# $? INSIDE the substitution reads the command that ran inside it.
status_inside_substitution() {
    echo "$(modal_separate "$@"; echo "inner=$?")"
}

# A separator starts a new command: here $? is the assignment's own status,
# which IS the substitution's, and that is what the author wants. This is the
# live shape in scripts/dmg_preflight.sh.
probe_status() {
    local out status=0
    out="$(pnpm --version)" || status=$?
    out=$(pnpm --version); status=$?
    echo "$out $status"
}

# Single quotes stop expansion entirely.
print_template() {
    printf '%s\n' '[$(date)] rc=$?'
}

# An escaped dollar is a literal, not a read.
print_escaped() {
    echo "$(date +%s) \$? is the status"
}
