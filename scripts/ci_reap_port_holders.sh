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
# A holder under a live job, or one that exits during the check, is the previous
# lock holder's teardown: the script waits up to MDT_CI_REAP_RELEASE_WAIT_S (30)
# for the port to free before it says anything else (Tue 8 Sep 2026: an engine
# on 8690 outlived its step by seconds and the next job reported a false "no CI
# provenance" on a pid that had just exited).
#
# A holder with NO CI provenance is a DIFFERENT population, not necessarily a
# stuck one (issue #1613): this host also runs the dispatcher's worker fleet
# as another unix user, and a fleet worktree's dynamic port claim can coincide
# with one of this repo's fixed suite ports (apps/webui/port_config.py now
# excludes them from allocation going forward, but an already-running
# worktree bound before that fix ships keeps whatever it already holds until
# it restarts). That population usually finishes on its own within minutes --
# run 34373773055 held port 8690 at 16:08:45Z and had freed it by 16:22Z with
# no human action -- so this script waits up to MDT_CI_REAP_FOREIGN_WAIT_S
# (120) for a foreign holder to release before failing the step, one retry
# rather than an immediate hand-off to a human. If a world-readable ownership
# marker exists for the port (written by apps/webui/port_config.py's
# claim_ports, issue #1613), it is named in both the wait log and the final
# error instead of the bare `<unreadable>` a cross-uid /proc lookup gives.
#
# Run this INSIDE the host lock, immediately before the servers bind: a live
# holder found before the lock can be cancelled and orphaned while this job
# waits for that lock, so the check has to sit between acquiring it and
# binding. The trailing command is exec'd once the ports are clear.
#
# The runner-side fix is the job-completed hook (fleet-af ci--hosts runner role), which kills
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
#   - if a holder without CI provenance releases within the foreign-holder
#     wait and the step still fails, or the command still does not run, then
#     broken
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

# Where apps/webui/port_config.py leaves a world-readable ownership marker
# for each dynamically claimed port (issue #1613): must match
# PORT_OWNER_REGISTRY_DIR there. Overridable so a test can point both at one
# throwaway directory without touching the real host-wide path.
readonly PORT_OWNER_REGISTRY_DIR="${MDT_CI_PORT_OWNER_REGISTRY_DIR:-/tmp/music-dj-tools-port-owners}"

#-----------------------------------------------------------------------------

# The socket probe is overridable for ONE reason: the collision this script
# exists for (issue #1613) is a listener owned by a DIFFERENT uid, which
# unprivileged `ss -p` reports without its pid. One uid cannot create that
# shape, so the test supplies the real `ss` minus the per-socket owner list --
# exactly the output the kernel/ss already produces cross-uid. Nothing else
# ever substitutes it, and production leaves it at `ss`.
readonly SS_BIN="${MDT_CI_REAP_SS:-ss}"

# Both helpers must yield an empty result, not a failed pipeline, on a free
# port: under `set -eo pipefail` a grep with no match would abort the script.
_holder_pids() {
    "$SS_BIN" -Hltnp "sport = :$1" | { grep -o 'pid=[0-9]*' || true; } | cut -d= -f2 | sort -u
}

_listener_count() {
    "$SS_BIN" -Hltn "sport = :$1" | { grep -c . || true; }
}

# Process identity: start time in clock ticks, field 22 of /proc/<pid>/stat
# (field 20 once "pid (comm) " is stripped, comm may contain spaces). Empty
# when the pid is gone.
# Empty (and exit 0) when the pid is gone: under `set -eo pipefail` a failing
# sed in a command substitution would abort the script on exactly the vanished
# pid this script exists to tolerate.
_identity() {
    { sed 's/^[0-9]* (.*) //' "/proc/$1/stat" 2>/dev/null || true; } | awk '{print $20}'
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
    PROVENANCE_SEEN="cwd=${cwd:-<unreadable>} exe=${exe:-<unreadable>}"
    case "$cwd/" in */_work/*) return 0 ;; esac
    case "$exe" in */_work/*) return 0 ;; esac
    return 1
}

# The previous lock holder's servers can outlive its step by a few seconds
# (engine teardown), so a port found held by a LIVE job right after taking the
# lock is usually mid-release. Wait for it, bounded, before deciding anything.
readonly RELEASE_WAIT_S="${MDT_CI_REAP_RELEASE_WAIT_S:-30}"
# A holder with no CI provenance belongs to a DIFFERENT population (issue
# #1613) and is usually gone within minutes, not seconds, so it gets its own,
# longer bound rather than sharing RELEASE_WAIT_S with the same-population
# teardown case above.
readonly FOREIGN_WAIT_S="${MDT_CI_REAP_FOREIGN_WAIT_S:-120}"
_wait_for_release() { # $1 = port, $2 = bound seconds (default RELEASE_WAIT_S) -> 0 once free, 1 if still held after the wait
    local port="$1" bound="${2:-$RELEASE_WAIT_S}" waited=0
    while [ "$waited" -lt "$bound" ]; do
        [ -z "$(_holder_pids "$port")" ] && [ "$(_listener_count "$port")" -eq 0 ] && { echo "[reap] port $port released after ${waited}s"; return 0; }
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

# Best-effort cross-uid attribution (issue #1613): apps/webui/port_config.py's
# claim_ports writes a world-readable marker per port it claims, keyed by
# port and the claimant's uid so concurrent uids never fight over one file in
# a sticky /tmp. A marker naming a worktree is a LEAD, not a live fact -- the
# claimant may already be gone -- so callers only ever use it to enrich a
# message, never to decide whether to signal anything.
_describe_registry_owner() { # $1 = port -> a one-line note, or empty
    local port="$1" newest="" newest_mtime=-1 candidate mtime
    for candidate in "$PORT_OWNER_REGISTRY_DIR/${port}-"*.owner; do
        [ -e "$candidate" ] || continue
        mtime=$(stat -c '%Y' "$candidate" 2>/dev/null || echo -1)
        if [ "$mtime" -gt "$newest_mtime" ]; then
            newest_mtime="$mtime"
            newest="$candidate"
        fi
    done
    [ -n "$newest" ] || return 0
    local worktree claimed_at
    worktree=$(grep -m1 '^worktree=' "$newest" 2>/dev/null | cut -d= -f2-)
    claimed_at=$(grep -m1 '^claimed_at=' "$newest" 2>/dev/null | cut -d= -f2-)
    [ -n "$worktree" ] || return 0
    echo "port-ownership registry: last dynamically claimed by worktree $worktree at ${claimed_at:-an unknown time} (issue #1613; not a CI job -- check that worktree, or run \`python3 -m apps.webui.port_config release\` there)"
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
# Ports whose ONE foreign-holder wait has already been spent in the first pass.
# The final pass must classify such a port again (it may have released during
# that wait) but must never wait a SECOND full FOREIGN_WAIT_S for the same
# holder: the verdict is already a failure, so the extra wait only holds the
# host lock longer to reach the same answer.
waited_ports=()

_already_waited() { # $1 = port
    case " ${waited_ports[*]:-} " in
        *" $1 "*) return 0 ;;
        *) return 1 ;;
    esac
}

for port in "${ports[@]}"; do
    pids=$(_holder_pids "$port")
    if [ -z "$pids" ]; then
        if [ "$(_listener_count "$port")" -gt 0 ]; then
            # The common shape of the actual #1613 collision: unprivileged
            # `ss -p` exposes a cross-uid listener but not its pid, so there
            # is no pid here to run the CI-provenance check on at all. Same
            # foreign population, same bounded retry and attribution.
            owner_note=$(_describe_registry_owner "$port")
            echo "[reap] port $port is held by a process this user cannot see (another uid)${owner_note:+ -- $owner_note}"
            echo "[reap] waiting up to ${FOREIGN_WAIT_S}s in case this is a foreign population finishing on its own (issue #1613)"
            if ! _wait_for_release "$port" "$FOREIGN_WAIT_S"; then
                echo "[ERROR] port $port is held by a process this user cannot see (another uid); nothing to reap, still held after ${FOREIGN_WAIT_S}s${owner_note:+. $owner_note}" >&2
                waited_ports+=("$port")
                status=1
            fi
        fi
        continue
    fi
    for pid in $pids; do
        identity=$(_identity "$pid")
        echo "[reap] port $port is held:"
        _describe "$pid"
        if [ -z "$identity" ] || ! _same_and_alive "$pid" "$identity"; then
            # It exited between the listing and here (the previous job's teardown).
            echo "[reap] holder pid $pid exited during the check"
            _wait_for_release "$port" || echo "[reap] port $port still held after ${RELEASE_WAIT_S}s; re-listing"
            continue
        fi
        if _has_live_ancestor "$pid"; then
            echo "[reap] holder is under a live job (ancestor matches /$LIVE_ANCESTOR_RE/); waiting up to ${RELEASE_WAIT_S}s for it to release"
            _wait_for_release "$port" || echo "[reap] port $port still held by a live job; left alone, the host lock serializes live suites"
            continue
        fi
        if ! _has_ci_provenance "$pid"; then
            if ! _same_and_alive "$pid" "$identity"; then
                echo "[reap] holder pid $pid exited while its provenance was being read"
                _wait_for_release "$port" || true
                continue
            fi
            owner_note=$(_describe_registry_owner "$port")
            echo "[reap] port $port holder pid $pid has no CI provenance (neither cwd nor executable under a runner _work tree; $PROVENANCE_SEEN); not a job leftover, not signalled${owner_note:+ -- $owner_note}"
            echo "[reap] waiting up to ${FOREIGN_WAIT_S}s in case this is a foreign population finishing on its own (issue #1613)"
            if _wait_for_release "$port" "$FOREIGN_WAIT_S"; then
                continue
            fi
            echo "[ERROR] port $port holder pid $pid has no CI provenance (neither cwd nor executable under a runner _work tree; $PROVENANCE_SEEN); not a job leftover, still held after ${FOREIGN_WAIT_S}s, not signalled${owner_note:+. $owner_note}. Free the port by hand." >&2
            waited_ports+=("$port")
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
# Final pass: a port still listening after the waits is classified once more,
# so a holder that appeared or survived the wait is either an orphan we reap
# now, a live job's (the lock's business), or an error, never a silent
# hand-off of an occupied port to the command below.
for port in "${ports[@]}"; do
    pids=$(_holder_pids "$port")
    if [ -z "$pids" ] && [ "$(_listener_count "$port")" -gt 0 ]; then
        owner_note=$(_describe_registry_owner "$port")
        if _already_waited "$port"; then
            echo "[ERROR] port $port is still held by a process this user cannot see after the foreign-holder wait${owner_note:+. $owner_note}" >&2
            status=1
        else
            echo "[reap] port $port still held by a process this user cannot see${owner_note:+ -- $owner_note}; waiting up to ${FOREIGN_WAIT_S}s more (issue #1613)"
            if ! _wait_for_release "$port" "$FOREIGN_WAIT_S"; then
                echo "[ERROR] port $port is still held by a process this user cannot see${owner_note:+. $owner_note}" >&2
                status=1
            fi
        fi
    fi
    for pid in $pids; do
        identity=$(_identity "$pid")
        [ -n "$identity" ] || continue
        if _has_live_ancestor "$pid"; then
            echo "[reap] port $port remains held by a live job after the wait; left to the host lock"
        elif _has_ci_provenance "$pid"; then
            echo "[reap] port $port orphan appeared during the wait; terminating pid $pid"
            _terminate "$pid" "$identity" || { echo "[ERROR] pid $pid survived SIGKILL" >&2; status=1; }
        elif ! _same_and_alive "$pid" "$identity"; then
            echo "[reap] port $port holder pid $pid exited during the final pass"
        else
            owner_note=$(_describe_registry_owner "$port")
            if _already_waited "$port"; then
                echo "[ERROR] port $port is still held by pid $pid with no CI provenance ($PROVENANCE_SEEN) after the foreign-holder wait${owner_note:+. $owner_note}" >&2
                status=1
                continue
            fi
            echo "[reap] port $port held after the wait by pid $pid with no CI provenance ($PROVENANCE_SEEN)${owner_note:+ -- $owner_note}; waiting up to ${FOREIGN_WAIT_S}s more (issue #1613)"
            if _wait_for_release "$port" "$FOREIGN_WAIT_S"; then
                continue
            fi
            echo "[ERROR] port $port still held after the wait by pid $pid with no CI provenance ($PROVENANCE_SEEN)${owner_note:+. $owner_note}" >&2
            status=1
        fi
    done
done
[ "$status" -eq 0 ] || exit "$status"
[ "$#" -gt 0 ] || exit 0
exec "$@"
