#!/usr/bin/env bash
#
# ci_reap_port_holders.sh -- free a suite's FIXED loopback ports of servers a
# previous job left behind, then (optionally) run the suite.
#
# Playwright starts each suite's backend and vite server as children of its own
# process. When the runner CANCELS a job mid-step (a superseding push on the
# same branch is the common trigger) the step's shell is killed, playwright
# dies with it, and the servers it spawned are reparented to init and keep
# their ports. Sun 6 Sep 2026 15:01 UTC: run 34040601527 was cancelled inside
# the comment-hotkey gate on agentbox-4; its `opendj-backend --port 8696`
# outlived the job, and every comment-hotkey step on the host for the next
# 17 minutes died with "address already in use", three of them on trunk.
#
# A holder is reaped only when BOTH hold:
#   provenance  its cwd or executable is under some runner's `_work` tree,
#               so it was started by a CI job on this host and nothing else;
#   orphaned    no Runner.Worker among its ancestors: every process of a live
#               job hangs under the runner's worker, so one reparented out of
#               that tree belongs to a job that is over.
# A holder under a live worker is reported and left alone: scripts/
# ci_host_lock.sh serializes live suites and this script must never kill
# another job's servers. A holder with no CI provenance is an ERROR, named,
# never signalled. Every signal re-checks the pid's identity (its start time
# from /proc/<pid>/stat) so a recycled pid is never hit.
#
# Run this INSIDE the host lock, immediately before the servers bind: a live
# holder found before the lock can be cancelled and orphaned while this job
# waits for that lock, so the check has to sit between acquiring it and
# binding. The trailing command is exec'd once the ports are clear.
#
# The runner-side fix is ops/ci/runner-hooks/job-completed.sh, which kills
# leftovers the moment a job ends. This is the in-repo net for a host where
# that hook is missing, and the place a holder gets NAMED.
#
# Usage:
#   scripts/ci_reap_port_holders.sh 5321 8696
#   scripts/ci_reap_port_holders.sh 5321 8696 -- pnpm exec playwright test ...
#
# Exit status: 0 when every listed port is free of orphans (a live holder does
# not fail the step); the command's own status when one is given; 1 when a
# holder has no CI provenance, cannot be identified, or survived SIGKILL;
# 2 on usage error.
#
# Regression lines:
#   - if a listed port is held by an orphan with CI provenance and it is still
#     alive afterwards then broken
#   - if a holder under a live worker, or one without CI provenance, is
#     signalled then broken
#   - if a holder without CI provenance does not fail the step then broken
#   - if the pid's identity changed between two signals and the second is
#     still sent then broken
#   - if no port is given then the step must fail rather than reap nothing
set -euo pipefail

ports=()
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
    ports+=("$1")
    shift
done
[ "$#" -eq 0 ] || shift   # drop the "--"; what remains is the command
if [ "${#ports[@]}" -lt 1 ]; then
    echo "[ERROR] usage: ci_reap_port_holders.sh PORT [PORT...] [-- COMMAND...]" >&2
    exit 2
fi

# What marks a live job's process tree. Overridable so the test can run this
# script from inside a job (where everything, the test included, is under a
# worker) and still exercise both branches.
readonly LIVE_ANCESTOR_RE="${MDT_CI_LIVE_ANCESTOR_RE:-Runner\.Worker}"

#-----------------------------------------------------------------------------

# Both helpers must yield an empty result, not a failed pipeline, on a free
# port: under `set -eo pipefail` a grep with no match would abort the script.
_holder_pids() {
    ss -Hltnp "sport = :$1" | { grep -o 'pid=[0-9]*' || true; } | cut -d= -f2 | sort -u
}

_listener_count() {
    ss -Hltn "sport = :$1" | { grep -c . || true; }
}

# Process identity: start time in clock ticks, field 22 of /proc/<pid>/stat
# (field 20 once "pid (comm) " is stripped, comm may contain spaces). Empty
# when the pid is gone.
_identity() {
    sed 's/^[0-9]* (.*) //' "/proc/$1/stat" 2>/dev/null | awk '{print $20}'
}

# Alive AND the same process as when we looked: exists, not a zombie (a
# signalled child its parent has not reaped still passes `kill -0`, but it
# holds no port and cannot be killed further), and the identity is unchanged.
_same_and_alive() {
    [ "$(_identity "$1")" = "$2" ] || return 1
    [ "$(ps -o stat= -p "$1" 2>/dev/null | cut -c1)" != "Z" ]
}

_has_ci_provenance() {
    local cwd exe
    cwd=$(readlink "/proc/$1/cwd" 2>/dev/null || true)
    exe=$(readlink "/proc/$1/exe" 2>/dev/null || true)
    case "$cwd/" in */_work/*) return 0 ;; esac
    case "$exe" in */_work/*) return 0 ;; esac
    return 1
}

_has_live_ancestor() {
    local pid="$1" args
    while [ "$pid" -gt 1 ]; do
        args=$(ps -o args= -p "$pid" 2>/dev/null) || return 1
        if printf '%s' "$args" | grep -Eq "$LIVE_ANCESTOR_RE"; then
            return 0
        fi
        pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ') || return 1
        [ -n "$pid" ] || return 1
    done
    return 1
}

_describe() {
    echo "    pid=$1 ppid=$(ps -o ppid= -p "$1" | tr -d ' ')" \
        "started='$(ps -o lstart= -p "$1")'" \
        "cwd=$(readlink "/proc/$1/cwd" 2>/dev/null || echo '?')" \
        "cmd=$(ps -o args= -p "$1" | cut -c1-160)"
}

# Terminate pid $1 whose identity is $2. Each signal is sent only to the
# process we identified; a recycled pid is left alone.
_terminate() {
    _same_and_alive "$1" "$2" && kill -TERM "$1" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        _same_and_alive "$1" "$2" || return 0
        sleep 0.5
    done
    echo "[reap] pid $1 ignored SIGTERM; sending SIGKILL"
    _same_and_alive "$1" "$2" && kill -KILL "$1" 2>/dev/null || true
    sleep 0.5
    ! _same_and_alive "$1" "$2"
}

#-----------------------------------------------------------------------------

status=0
for port in "${ports[@]}"; do
    pids=$(_holder_pids "$port")
    if [ -z "$pids" ]; then
        if [ "$(_listener_count "$port")" -gt 0 ]; then
            echo "[ERROR] port $port is held by a process this user cannot see (another uid); nothing to reap, the suite will collide" >&2
            status=1
        fi
        continue
    fi
    for pid in $pids; do
        identity=$(_identity "$pid")
        echo "[reap] port $port is held:"
        _describe "$pid"
        if _has_live_ancestor "$pid"; then
            echo "[reap] holder is under a live job (ancestor matches /$LIVE_ANCESTOR_RE/); left alone, the host lock serializes live suites"
            continue
        fi
        if ! _has_ci_provenance "$pid"; then
            echo "[ERROR] port $port holder pid $pid has no CI provenance (neither cwd nor executable under a runner _work tree); not a job leftover, not signalled. Free the port by hand." >&2
            status=1
            continue
        fi
        echo "[reap] orphan of a finished or cancelled CI job (no live ancestor, cwd/exe under _work); terminating"
        if ! _terminate "$pid" "$identity"; then
            echo "[ERROR] pid $pid survived SIGKILL" >&2
            status=1
        fi
    done
done
[ "$status" -eq 0 ] || exit "$status"
[ "$#" -gt 0 ] || exit 0
exec "$@"
