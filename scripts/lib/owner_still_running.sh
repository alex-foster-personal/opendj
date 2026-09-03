# Zombie-aware "is this PID still able to act" probe.
#
# WHY SHARED. scripts/autoreposync.sh (the daemon's own startup sweep, pidfile
# gate, and stop path) and scripts/autoreposync_prompt_hook.sh (the health
# check an agent's prompt reads) both have to answer the same question about a
# recorded pid, and a `kill -0` alone answers it wrong: `kill -0` succeeds for
# a ZOMBIE, and a zombie can act on nothing - it cannot end its bounded group,
# publish a record, run a trap, or keep a daemon alive. A predecessor killed
# with -9 sits in exactly that state until its parent reaps it, and a parent
# that is itself parked - a login shell in a foreground command, a container
# PID 1 that does not reap orphans - can leave it there indefinitely. Two
# hand-rolled copies of this check drift the same way lib/macho.sh's
# comment warns a duplicated Mach-O predicate drifted: one narrows, the other
# does not, and only one of them gets fixed. Codex found the zombie class
# against the daemon's own pidfile gate on #717 and against the prompt hook's
# health check on #719.
#
# An UNREADABLE state is treated as running, deliberately. This answer decides
# whether the caller may act as though nobody holds the pid - signal a process
# group, or tell an agent the daemon is down - and a `ps` that cannot say must
# not be read as permission to do either.
#
# Source it, do not execute it:
#   . "scripts/lib/owner_still_running.sh"  # from the repo root (autoreposync.sh)
#   . "$helper_path"                         # from the hook: active worktree first,
#                                             # canonical checkout only for the deployed
#                                             # ops/autoreposync/prompt-hook.sh copy
#
# POSIX sh, no bashisms: the prompt hook sources this under plain `sh`, not
# bash. NO `set` OPTIONS IN THIS FILE - sourcing mutates the CALLER's shell,
# so options here would leak into whichever script sourced it.
owner_still_running() { # owner_still_running PID -> 0 if PID can still act
    kill -0 "$1" 2>/dev/null || return 1
    case "$(ps -o state= -p "$1" 2>/dev/null | tr -d ' ')" in
        Z*) return 1 ;;
    esac
    return 0
}
